"""Where each delivering hook turn came to: one content-free line per turn.

Inside a hook, an empty answer and a dead selector look alike, and the agent sees
neither. Each turn of the two hooks that deliver memory therefore lands on exactly
one of ``OUTCOMES``, read from what the hook itself emitted, and appends one line to
``logs/hook-routes.jsonl``. Lines hold no prompt, statement, record id or domain.

A hook that runs under the bridge hands its line to the bridge, because only the
bridge knows whether the host received the output, and it alone sees the failures
that happen outside this process. A hook run on its own writes its line itself.

This module is imported by every hook turn, so it must not import the Jev module.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from imprint.config import effective_selector_mode
from imprint.log_files import append_capped, newest_lines

ROUTE_LOG_NAME = "hook-routes.jsonl"
ROUTE_LOG_MAX_BYTES = 5 * 1024 * 1024
ROUTE_LOG_TAIL_BYTES = 2 * 1024 * 1024
# How far back health looks, for its counts and for a run of failures.
ROUTE_WINDOW_SECONDS = 24 * 60 * 60
ROUTE_DEFER_ENV = "IMPRINT_DEFER_ROUTE_LINE"
ROUTE_FIELD = "_imprint_route"

OUTCOMES = ("delivered", "nothing_relevant", "already_delivered", "scratch_skip", "failed")
ROUTES = {"session-start": "session_start", "user-prompt-submit": "user_prompt_submit"}

# The selector_outcome of a turn T3's scratch check kept from Jev.
SCRATCH_OUTCOME = "t3_scratch_thread"
# Nothing to send is an answer about the store, not about Jev.
EMPTY_POOL_REASON = "no_candidates"
HOOK_INPUT_INVALID = "hook_input_invalid"
# Jev picked records and none could be delivered.
_UNDELIVERED_PICKS = frozenset({"crowded_picks", "oversized_picks"})


def route_log_path(root: Path) -> Path:
    return Path(root) / "logs" / ROUTE_LOG_NAME


def _outcome(body: Mapping[str, Any], payload: str, delivery: Mapping[str, Any] | None) -> tuple[str, object]:
    """The one outcome of a hook's output, and the closed code that explains it.

    The order is the ruling. A scratch chat never reached Jev and a failed selection
    lost Jev's turn, whatever the deterministic path then delivered, so both come
    before delivery, and the line's ``bytes`` says what went out anyway. Any reason
    but an empty pool is a failure, so one added later cannot pass as an empty turn.

    ``delivery`` is the retrieval behind the output, which on a repeated turn is the
    committed receipt, or None if that cannot be read. The output cannot tell a latch
    that delivered from one that did not, because an empty turn commits one too. A
    repeat of an empty turn comes to what that turn came to.
    """
    selector_outcome = body.get("selector_outcome")
    reason = body.get("fallback_reason")
    if selector_outcome == SCRATCH_OUTCOME:
        return "scratch_skip", None
    if reason not in (None, EMPTY_POOL_REASON):
        return "failed", reason
    if body.get("reason") == HOOK_INPUT_INVALID:
        return "failed", HOOK_INPUT_INVALID
    if body.get("status") == "already_delivered":
        if delivery is None or delivery.get("selected_ids"):
            return "already_delivered", None
        selector_outcome = delivery.get("selector_outcome", selector_outcome)
    if payload:
        return "delivered", reason or selector_outcome
    if selector_outcome in _UNDELIVERED_PICKS:
        return "failed", selector_outcome
    return "nothing_relevant", reason or selector_outcome or body.get("reason")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def route_entry(
    route: str, body: Mapping[str, Any], *, mode: str, answered: bool = False,
    delivery: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """The line for one hook output. ``answered`` means Jev replied on this turn."""
    context = body.get("hookSpecificOutput")
    payload = context.get("additionalContext") if isinstance(context, Mapping) else ""
    payload = payload if isinstance(payload, str) else ""
    outcome, detail = _outcome(body, payload, delivery)
    return {
        "answered": answered,
        "bytes": len(payload.encode("utf-8", "replace")),
        "detail": detail,
        "method": body.get("selector_method", "deterministic"),
        "mode": mode,
        "outcome": outcome,
        "route": route,
        "ts": _stamp(),
    }


def failed_entry(action: str, error: str, *, mode: str) -> dict[str, object]:
    """The line for a turn that ended before the hook could say how it went."""
    return {
        "answered": False, "bytes": 0, "detail": error, "method": None, "mode": mode,
        "outcome": "failed", "route": ROUTES[action], "ts": _stamp(),
    }


def record_route(root: Path, entry: Mapping[str, object]) -> None:
    """Append one line, rotating once at the size cap. Never raises."""
    try:
        append_capped(route_log_path(root), entry, ROUTE_LOG_MAX_BYTES)
    except Exception:
        pass


class HookTurn:
    """One turn of a delivering hook. Nothing here can change what the hook emits."""

    def __init__(self, root: Path, action: str, config: Mapping[str, Any]):
        self._root = root
        self._action = action
        self._mode = effective_selector_mode(config.get("selector"))
        self._deferred = os.environ.get(ROUTE_DEFER_ENV) == "1"
        self._entry: dict[str, object] | None = None
        self.answered = False
        self.delivery: Mapping[str, Any] | None = None

    def settle(self, body: dict[str, Any]) -> dict[str, Any]:
        """Note where the turn came to, and return the output to emit."""
        try:
            entry = route_entry(
                ROUTES[self._action], body, mode=self._mode, answered=self.answered, delivery=self.delivery,
            )
        except Exception:
            return body
        if self._deferred:
            return {**body, ROUTE_FIELD: entry}
        self._entry = entry
        return body

    def abandon(self) -> None:
        """The turn raised after settling, so what it settled on did not happen."""
        self._entry = None

    def close(self) -> None:
        if not self._deferred:
            record_route(self._root, self._entry or failed_entry(self._action, "hook_exception", mode=self._mode))


def route_summary(root: Path, *, now: float | None = None) -> dict[str, object]:
    """Last-day outcome counts per route, and the run of failed selections the log ends on.

    A turn Jev answered ends a run. A turn that never asked Jev says nothing about it,
    so it neither adds to a run nor ends one. Damaged lines count as nothing, and so do
    stamps outside the window, which is how a store's history ages out.
    """
    current = time.time() if now is None else now
    outcomes = {route: dict.fromkeys(OUTCOMES, 0) for route in ROUTES.values()}
    failures = 0
    for raw in newest_lines(route_log_path(root), ROUTE_LOG_TAIL_BYTES):
        try:
            entry = json.loads(raw)
            age = current - datetime.fromisoformat(entry["ts"].replace("Z", "+00:00")).timestamp()
            if not 0 <= age <= ROUTE_WINDOW_SECONDS:
                continue
            outcomes[entry["route"]][entry["outcome"]] += 1
        except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError):
            continue
        if entry.get("mode") != "jev":
            continue
        if entry.get("answered") is True:
            failures = 0
        elif entry["outcome"] == "failed" and entry.get("method") == "deterministic_fallback":
            failures += 1
    return {"outcomes_24h": outcomes, "selector_failures_in_a_row": failures}


__all__ = [
    "HookTurn",
    "OUTCOMES",
    "ROUTES",
    "ROUTE_DEFER_ENV",
    "ROUTE_FIELD",
    "ROUTE_WINDOW_SECONDS",
    "failed_entry",
    "record_route",
    "route_entry",
    "route_log_path",
    "route_summary",
]
