"""The one line a prompt turn carries when judgment selection failed.

Inside a hook an empty context and a dead selector look alike, so an agent cannot
tell "nothing applies" from "memory is down". With
``org.imprint.selector.failure_notice`` on, a ``jev`` prompt turn whose selection
failed and that delivered nothing else carries this line instead of nothing. It is
fixed text plus one closed reason: no prompt, statement, record or path.

The prompt hook imports this module on every turn, so it must not import the Jev
module. Its reasons are the selector's fallback reasons less the empty pool, which
is an answer about the store, not a failure; a test keeps the two lists equal.
"""

from __future__ import annotations

NOTICE_REASONS = frozenset({
    "missing_key", "key_file_unsafe", "timeout", "http_error", "bad_response", "exception",
})
# The hook output field that says the line went out, which the route line repeats.
NOTICE_FIELD = "selector_notice"
NOTICE_CODE = "selection_failed"
_TEMPLATE = "Imprint: judgment retrieval was unavailable this turn (reason: {reason}).\n"


def failure_notice(reason: object) -> str | None:
    """The notice for a closed failure reason, or None for anything else."""
    if not isinstance(reason, str) or reason not in NOTICE_REASONS:
        return None
    return _TEMPLATE.format(reason=reason)


def with_failure_notice(response: dict, *, enabled: bool) -> dict:
    """Return ``response`` with the notice in place of empty context, when it applies.

    It applies only when the switch is on, the turn fell back from ``jev`` (never
    ``shadow``, which delivers the deterministic answer by design), the reason is a
    failure, and nothing else is in the context. Otherwise ``response`` comes back
    unchanged, so with the switch off no byte differs.
    """
    if enabled is not True or response.get("selector_method") != "deterministic_fallback":
        return response
    notice = failure_notice(response.get("fallback_reason"))
    output = response.get("hookSpecificOutput")
    if notice is None or not isinstance(output, dict) or output.get("additionalContext"):
        return response
    return {**response, NOTICE_FIELD: NOTICE_CODE, "hookSpecificOutput": {**output, "additionalContext": notice}}


__all__ = ["NOTICE_CODE", "NOTICE_FIELD", "NOTICE_REASONS", "failure_notice", "with_failure_notice"]
