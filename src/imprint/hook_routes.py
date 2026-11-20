"""Where each hook turn came to: one content-free line per turn.

Inside a hook, an empty answer and a dead selector look alike, and the agent sees
neither. Each turn of the two hooks that deliver memory therefore lands on exactly
one of ``OUTCOMES``, read from what the hook itself emitted and, where that cannot say,
from the retrieval behind it, and appends one line to ``logs/hook-routes.jsonl``.
A Stop that captured nothing looks the same whether nobody gave feedback or every
prompt was refused, so each Stop lands on one of ``STOP_OUTCOMES`` and appends its
line to the same file. Lines hold no prompt, statement, record id or domain.

A hook that runs under the bridge hands its line to the bridge, because only the
bridge knows whether the host received the output, and it alone sees the failures
that happen outside this process. A hook run on its own writes its line itself.

This module is imported by every hook turn, so it must not import the Jev module.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from imprint.config import effective_selector_mode
from imprint.log_files import append_capped, newest_lines
from imprint.selection_notice import NOTICE_FIELD

ROUTE_LOG_NAME = "hook-routes.jsonl"
ROUTE_LOG_MAX_BYTES = 5 * 1024 * 1024
ROUTE_LOG_TAIL_BYTES = 2 * 1024 * 1024
# How far back health looks, for its counts and for a run of failures.
ROUTE_WINDOW_SECONDS = 24 * 60 * 60
ROUTE_DEFER_ENV = "IMPRINT_DEFER_ROUTE_LINE"
ROUTE_FIELD = "_imprint_route"

OUTCOMES = ("delivered", "nothing_relevant", "already_delivered", "scratch_skip", "failed")
STOP_ROUTE = "stop_capture"
ROUTES = {"session-start": "session_start", "user-prompt-submit": "user_prompt_submit", "stop-capture": STOP_ROUTE}
# Each reason the stop hook skips a turn for is an outcome of its own. A reason not
# listed is a failure, so one added later cannot pass as a skip. Written out rather
# than imported: every hook turn imports this module.
STOP_SKIP_OUTCOMES = (
    "feedback_text_unavailable", "machine_origin", "no_operator_message", "operator_text_oversized",
    "operator_turn_already_captured", "operator_turn_expired", "synthetic_transcript_entry",
    "t3_entrypoint_unknown", "t3_marked_window", "t3_prompt_unmatched", "t3_record_unavailable",
    "t3_scratch_thread", "t3_session_ref_unreadable", "t3_session_unmatched",
    "transcript_malformed", "transcript_refused", "transcript_tail_exhausted", "transcript_unavailable",
)
STOP_OUTCOMES = ("captured", "not_feedback", "origin_unverified", *STOP_SKIP_OUTCOMES, "failed")
# The two skips the stop hook's receipt names otherwise.
_STOP_RENAMED = {"not_explicit_feedback": "not_feedback", "unverified_provenance": "origin_unverified"}
_STOP_SKIPS = frozenset(STOP_SKIP_OUTCOMES) | frozenset(_STOP_RENAMED.values())
UNKNOWN_STOP_REASON = "unknown_reason"
# Stops that could not have captured whatever was said: automation, and T3's scratch
# folder, where no prompt is credited. The silence signal does not count them.
_STOPS_THAT_CANNOT_CAPTURE = frozenset({"machine_origin", "t3_scratch_thread"})
_ROUTE_OUTCOMES = {"session_start": OUTCOMES, "user_prompt_submit": OUTCOMES, STOP_ROUTE: STOP_OUTCOMES}

# The selector_outcome of a turn T3's scratch check kept from Jev.
SCRATCH_OUTCOME = "t3_scratch_thread"
# Nothing to send is an answer about the store, not about Jev.
EMPTY_POOL_REASON = "no_candidates"
HOOK_INPUT_INVALID = "hook_input_invalid"
# Jev picked records and none could be delivered.
_UNDELIVERED_PICKS = frozenset({"crowded_picks", "oversized_picks"})
# Records were due on this turn and none could fit. Neither says anything about Jev,
# and neither is the same as nothing being relevant.
UNFIT_RECORDS = "oversized_records"
# Health's share of failed selections reads at most this many of the newest, and says
# nothing below the minimum, where one timeout would swing it.
SELECTOR_SHARE_WINDOW = 50
SELECTOR_SHARE_MIN_SAMPLE = 10


def route_log_path(root: Path) -> Path:
    return Path(root) / "logs" / ROUTE_LOG_NAME


def _stage(delivery: Mapping[str, Any] | None, name: str) -> int:
    stages = delivery.get("selector_stages") if delivery is not None else None
    value = stages.get(name) if isinstance(stages, Mapping) else None
    return value if isinstance(value, int) else 0


def _could_not_fit(delivery: Mapping[str, Any] | None) -> bool:
    """Whether records were due and none went out.

    Under Jev ``omitted_count`` also holds what Jev passed over, so there only the
    records the local path left out can say this.
    """
    if delivery is None or delivery.get("selected_ids"):
        return False
    if isinstance(delivery.get("selector_stages"), Mapping):
        return _stage(delivery, "local_unfit") > 0
    omitted = delivery.get("omitted_count")
    return isinstance(omitted, int) and omitted > 0


def _outcome(
    body: Mapping[str, Any], payload: str, delivery: Mapping[str, Any] | None, held: bool,
) -> tuple[str, object]:
    """The one outcome of a hook's output, and the closed code that explains it.

    The order is the ruling. A scratch chat never reached Jev and a failed selection
    lost Jev's turn, whatever the deterministic path then delivered, so both come
    before delivery, and the line's ``bytes`` says what went out anyway. Any reason
    but an empty pool is a failure, so one added later cannot pass as an empty turn.

    ``delivery`` is the retrieval behind the output, which on a repeated turn is the
    committed receipt, or None if that cannot be read. The output cannot tell a latch
    that delivered from one that did not, because an empty turn commits one too. A
    repeat of an empty turn comes to what that turn came to. ``held`` says nothing was
    left to send because this session already has it, which a receipt keeps as a stage.
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
    if _could_not_fit(delivery):
        return "failed", UNFIT_RECORDS
    if held or _stage(delivery, "session_held"):
        return "already_delivered", None
    return "nothing_relevant", reason or selector_outcome or body.get("reason")


def _stop_outcome(body: Mapping[str, Any]) -> tuple[str, object]:
    """The one outcome of a stop hook's output, and the closed code that explains it.

    A written envelope is a capture, whether or not it compiled, and the detail says
    which. A skip is its reason, with the verdict basis behind it where the receipt
    gives one. Input a retry cannot change is a failure, as on the other routes.
    """
    status = body.get("status")
    reason = body.get("reason")
    if status == "queued":
        return "captured", body.get("canonical_status")
    if status != "skipped" or not isinstance(reason, str):
        return "failed", UNKNOWN_STOP_REASON
    if reason == HOOK_INPUT_INVALID:
        return "failed", HOOK_INPUT_INVALID
    outcome = _STOP_RENAMED.get(reason, reason)
    if outcome not in _STOP_SKIPS:
        return "failed", UNKNOWN_STOP_REASON
    return outcome, body.get("provenance_basis")


def _stamp() -> str:
    """The time of a memory hook's line, to the second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stop_stamp() -> str:
    """The time of a Stop line, to the second.

    It reads ``time.time()`` and not ``datetime.now()``. The byte-identity replay
    steps its datetime clock on every read, so a Stop that read it once more would
    move the stamp of every record after it, and nothing else would change.
    """
    return datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stop_entry(body: Mapping[str, Any], *, mode: str | None) -> dict[str, object]:
    """The line for one Stop output.

    A Stop delivers nothing and never asks Jev, so its line has no bytes, no method
    and is never answered. ``mode`` is None only on a Stop skipped before the config
    was read.
    """
    outcome, detail = _stop_outcome(body)
    return {
        "answered": False, "bytes": 0, "detail": detail, "method": None, "mode": mode,
        "outcome": outcome, "route": STOP_ROUTE, "ts": _stop_stamp(),
    }


def route_entry(
    route: str, body: Mapping[str, Any], *, mode: str | None, answered: bool = False,
    delivery: Mapping[str, Any] | None = None, held: bool = False,
) -> dict[str, object]:
    """The line for one output of a hook that delivers memory. ``answered`` means
    Jev replied on this turn. A Stop's line comes from ``stop_entry``."""
    if route == STOP_ROUTE:
        raise ValueError("a Stop's route line comes from stop_entry")
    context = body.get("hookSpecificOutput")
    payload = context.get("additionalContext") if isinstance(context, Mapping) else ""
    payload = payload if isinstance(payload, str) else ""
    outcome, detail = _outcome(body, payload, delivery, held)
    return {
        "answered": answered,
        "bytes": len(payload.encode("utf-8", "replace")),
        "detail": detail,
        "method": body.get("selector_method", "deterministic"),
        "mode": mode,
        "outcome": outcome,
        "route": route,
        "ts": _stamp(),
        # Only a turn that carried the failure notice says so, so a line is otherwise unchanged.
        **({"notice": True} if body.get(NOTICE_FIELD) else {}),
    }


def failed_entry(action: str, error: str, *, mode: str) -> dict[str, object]:
    """The line for a turn that ended before the hook could say how it went, on the
    clock its route's own lines use."""
    route = ROUTES[action]
    return {
        "answered": False, "bytes": 0, "detail": error, "method": None, "mode": mode,
        "outcome": "failed", "route": route, "ts": _stop_stamp() if route == STOP_ROUTE else _stamp(),
    }


def record_route(root: Path, entry: Mapping[str, object]) -> None:
    """Append one line, rotating once at the size cap. Never raises."""
    try:
        append_capped(route_log_path(root), entry, ROUTE_LOG_MAX_BYTES)
    except Exception:
        pass


class HookTurn:
    """One turn of a hook with a route. Nothing here can change what the hook emits."""

    def __init__(self, root: Path, action: str, config: Mapping[str, Any]):
        self._root = root
        self._action = action
        self._mode = effective_selector_mode(config.get("selector"))
        self._deferred = os.environ.get(ROUTE_DEFER_ENV) == "1"
        self._entry: dict[str, object] | None = None
        self._kept: Mapping[str, Any] | None = None
        self.answered = False
        self.delivery: Mapping[str, Any] | None = None
        self.held = False

    def settle(self, body: dict[str, Any]) -> dict[str, Any]:
        """Note where the turn came to, and return the output to emit."""
        try:
            route = ROUTES[self._action]
            entry = stop_entry(body, mode=self._mode) if route == STOP_ROUTE else route_entry(
                route, body, mode=self._mode, answered=self.answered, delivery=self.delivery, held=self.held,
            )
        except Exception:
            return body
        if self._deferred:
            return {**body, ROUTE_FIELD: entry}
        self._entry = entry
        return body

    def keep(self, body: Mapping[str, Any]) -> None:
        """A Stop wrote its envelope, which no later failure takes back.

        ``body`` is its receipt, read only if the turn then fails, so it says how
        far the capture got.
        """
        self._kept = body

    def abandon(self) -> dict[str, Any]:
        """The turn raised after settling, so what it settled on did not happen.

        A Stop that kept its receipt still captured, so its line says so. Returns
        what the failure's output must carry: under the bridge, that line.
        """
        self._entry = None
        if self._kept is not None:
            try:
                self._entry = stop_entry(self._kept, mode=self._mode)
            except Exception:
                pass
        return {ROUTE_FIELD: self._entry} if self._deferred and self._entry is not None else {}

    def close(self) -> None:
        if not self._deferred:
            record_route(self._root, self._entry or failed_entry(self._action, "hook_exception", mode=self._mode))


def _failure_share(selections: deque) -> dict[str, object]:
    sample, failed = len(selections), sum(selections)
    return {
        "window": SELECTOR_SHARE_WINDOW,
        "min_sample": SELECTOR_SHARE_MIN_SAMPLE,
        "sample": sample,
        "failed": failed,
        "share": round(failed / sample, 4) if sample >= SELECTOR_SHARE_MIN_SAMPLE else None,
    }


def route_summary(
    root: Path, *, now: float | None = None, stop_window_seconds: int | None = None,
) -> dict[str, object]:
    """Last-day outcome counts per route, and the run of failed selections the log ends on.

    A turn that used Jev's answer ends a run. A failed selection adds to one, even if it
    failed before it could ask or after Jev replied, because the agent got no picks either
    way. Any other turn says nothing about selection, so it neither adds to a run nor
    ends one. Damaged lines count as nothing, and so do stamps outside the
    window, which is how a store's history ages out.

    The same selections, the newest ``SELECTOR_SHARE_WINDOW`` of them, give the share
    that failed, which shows a Jev that fails on and off. It is only reported: what
    degrades health is still the run alone.

    With ``stop_window_seconds``, it also counts, in that window and as far back as
    the tail it reads, the Stop lines that could have captured and those that did.
    """
    current = time.time() if now is None else now
    outcomes = {route: dict.fromkeys(_ROUTE_OUTCOMES[route], 0) for route in ROUTES.values()}
    failures = 0
    selections: deque = deque(maxlen=SELECTOR_SHARE_WINDOW)
    stop_calls = 0
    stop_captures = 0
    for raw in newest_lines(route_log_path(root), ROUTE_LOG_TAIL_BYTES):
        try:
            entry = json.loads(raw)
            age = current - datetime.fromisoformat(entry["ts"].replace("Z", "+00:00")).timestamp()
            route, outcome = entry["route"], entry["outcome"]
            if (
                stop_window_seconds is not None and route == STOP_ROUTE
                and outcome in STOP_OUTCOMES and 0 <= age <= stop_window_seconds
            ):
                stop_calls += outcome not in _STOPS_THAT_CANNOT_CAPTURE
                stop_captures += outcome == "captured"
            if not 0 <= age <= ROUTE_WINDOW_SECONDS:
                continue
            outcomes[route][outcome] += 1
        except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError):
            continue
        if entry.get("mode") != "jev":
            continue
        if entry.get("answered") is True and entry.get("method") == "jev":
            failures = 0
            selections.append(False)
        elif (
            entry["outcome"] == "failed" and entry.get("method") == "deterministic_fallback"
            and entry.get("detail") != UNFIT_RECORDS
        ):
            failures += 1
            selections.append(True)
    summary: dict[str, object] = {
        "outcomes_24h": outcomes,
        "selector_failures_in_a_row": failures,
        "selector_failure_share": _failure_share(selections),
    }
    if stop_window_seconds is not None:
        summary["stop_calls_in_window"] = stop_calls
        summary["stop_captures_in_window"] = stop_captures
    return summary


__all__ = [
    "HookTurn",
    "OUTCOMES",
    "ROUTES",
    "ROUTE_DEFER_ENV",
    "ROUTE_FIELD",
    "ROUTE_WINDOW_SECONDS",
    "SELECTOR_SHARE_MIN_SAMPLE",
    "SELECTOR_SHARE_WINDOW",
    "STOP_OUTCOMES",
    "STOP_ROUTE",
    "STOP_SKIP_OUTCOMES",
    "UNFIT_RECORDS",
    "UNKNOWN_STOP_REASON",
    "failed_entry",
    "record_route",
    "route_entry",
    "route_log_path",
    "route_summary",
    "stop_entry",
]
