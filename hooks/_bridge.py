"""Portable Claude Code hook bridge with private failure diagnostics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

HOOK_TIMEOUT_SECONDS = 10
_EVENT_NAMES = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "health-check": "SessionStart",
}


def _persist_failure(action: str, process: subprocess.CompletedProcess[str]) -> None:
    """Best-effort private diagnostics for a wrapped action failure."""
    try:
        from imprint.config import load_config, resolved_operator_root
        from imprint.permissions import secure_directory, secure_file

        clock = datetime.now(timezone.utc)
        directory = resolved_operator_root(load_config()) / "logs" / "hook-failures"
        secure_directory(directory)
        path = directory / f"{clock.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}.json"
        content = json.dumps({
            "timestamp": clock.isoformat().replace("+00:00", "Z"),
            "action": action,
            "exit_code": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }, sort_keys=True, ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        secure_file(path)
    except Exception:
        pass


def _failure(action: str, error: str, *, stop_hook_active: bool = False) -> int:
    """Stop fails closed only on a definite capture failure; everything else
    fails open visibly.

    Exit 2 blocks the host's stop, so it is reserved for the one error a
    blocked stop can actually heal: the capture subprocess reported failure.
    Invalid input, a missing or hung executable, and malformed output are not
    healed by retrying -- exiting 2 on those wedges the host in a stop loop
    whenever the install is degraded. A block also never repeats once
    stop_hook_active says a previous block already happened.
    """
    blocking = (
        action == "stop-capture"
        and error == "hook_action_failed"
        and not stop_hook_active
    )
    body = {
        "hook_schema_version": "1.0.0",
        "status": "degraded",
        "error": error,
        "failure_policy": "fail_closed" if blocking else "fail_open",
    }
    if action != "stop-capture":
        body["hookSpecificOutput"] = {
            "hookEventName": _EVENT_NAMES[action],
            "additionalContext": "",
        }
    print(json.dumps(body, sort_keys=True))
    if blocking:
        # On exit 2 the host feeds stderr back to the model; stdout is ignored.
        print(
            "imprint: judgment capture failed (hook_action_failed); "
            "blocking this stop once so the capture is not silently lost.",
            file=sys.stderr,
        )
        return 2
    return 0


def run(action: str) -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _failure(action, "hook_input_invalid")
    stop_hook_active = isinstance(event, dict) and bool(event.get("stop_hook_active"))
    try:
        process = subprocess.run(
            [sys.executable, "-m", "imprint.cli", "hook", action],
            input=json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=False,
            timeout=HOOK_TIMEOUT_SECONDS,
            env=dict(os.environ, IMPRINT_DEFER_DELIVERY_COMMIT="1"),
        )
    except subprocess.TimeoutExpired:
        return _failure(action, "hook_action_timeout", stop_hook_active=stop_hook_active)
    except OSError:
        return _failure(action, "hook_executable_unavailable", stop_hook_active=stop_hook_active)
    body = None
    valid_degraded_health = False
    if action == "health-check" and process.returncode == 2 and process.stdout:
        try:
            body = json.loads(process.stdout)
        except json.JSONDecodeError:
            pass
        valid_degraded_health = isinstance(body, dict) and body.get("status") == "degraded"
    if process.returncode and not valid_degraded_health:
        _persist_failure(action, process)
        return _failure(action, "hook_action_failed", stop_hook_active=stop_hook_active)
    if process.stdout:
        if body is None:
            try:
                body = json.loads(process.stdout)
            except json.JSONDecodeError:
                return _failure(action, "hook_output_invalid", stop_hook_active=stop_hook_active)
        if valid_degraded_health:
            reasons = body.get("degraded_reasons")
            concise_reasons = [
                reason for reason in reasons
                if isinstance(reason, str) and reason
            ] if isinstance(reasons, list) else []
            summary = "imprint health: degraded"
            if concise_reasons:
                summary += " — " + ", ".join(concise_reasons)
            hook_output = body.get("hookSpecificOutput")
            hook_output = dict(hook_output) if isinstance(hook_output, dict) else {}
            hook_output.update({
                "hookEventName": "SessionStart",
                "additionalContext": summary,
            })
            body["hookSpecificOutput"] = hook_output
        delivery = body.pop("_imprint_delivery", None) if isinstance(body, dict) else None
        sys.stdout.write(json.dumps(body, sort_keys=True) + "\n")
        sys.stdout.flush()
        if delivery is not None:
            # The payload is already visible to the hook consumer. A failed
            # commit intentionally leaves the pending cache for replay.
            try:
                subprocess.run(
                    [sys.executable, "-m", "imprint.cli", "delivery-commit"],
                    input=json.dumps(delivery, sort_keys=True), text=True,
                    capture_output=True, check=False, timeout=HOOK_TIMEOUT_SECONDS,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
    return 0
