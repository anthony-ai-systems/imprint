"""One typed Jev call: a state and named questions in, typed answers out.

``ask`` never raises. A purpose that is off makes no request and touches no file:
it returns a ``disabled`` result. Otherwise every call writes one content-free line
to its purpose's decision log, whatever came of it.

The transport rules are the selector's own, imported from ``imprint.retrieve.jev``
rather than copied, so the two cannot drift: the key comes from the variable or the
private file ``selector.jev`` names and is sent only in the ``Authorization``
header, redirects are refused, the response is bounded, and one wall-clock deadline
covers key resolution, the request and parsing. The endpoint and default model are
``selector.jev``'s too, whatever ``selector.mode`` says. Nothing here retries.

The selector does not use this module, and no hook imports it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

from imprint import __version__
from imprint.log_files import append_capped
from imprint.permissions import secure_directory, secure_file
from imprint.retrieve import jev as _jev

# Private names, imported on purpose: they are the selector's transport rules.
from imprint.retrieve.jev import (
    JevSettings,
    _BadResponse,
    _DeadlineExceeded,
    _echoed_model,
    _failure_reason,
    _KeyUnavailable,
    _run_with_deadline,
    _usage,
    resolve_api_key,
)

from .purposes import purpose_mode
from .questions import NAME, QUESTION_TYPES, Answer, Question

STATE_LIMIT = 16000
STATE_DEPTH_LIMIT = 64
MAX_QUESTIONS = 1000
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 30
DECISION_LOG_MAX_BYTES = 5 * 1024 * 1024
COUNTER_NAME = "decide-calls.json"
COUNTER_LOCK_NAME = "decide-calls.lock"
MAX_COUNTER_BYTES = 4096
OUTCOMES = ("answered", "disabled", "capped", "fallback")
REASONS = frozenset({
    # disabled
    "unknown_purpose", "switched_off", "purpose_off",
    # capped
    "cap_reached", "cap_unavailable",
    # fallback
    "invalid_request", "missing_key", "key_file_unsafe", "timeout", "http_error", "bad_response", "exception",
})
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_SHAPE_FIELDS = 16


class _Invalid(Exception):
    pass


@dataclass(frozen=True)
class DecisionResult:
    """What one call came to. ``outcome`` is one of ``OUTCOMES``.

    ``answers`` holds a typed answer for each question that got a valid one, and
    ``dropped`` names the questions sent that did not. ``shapes`` gives, for every
    answer the reply carried, its field names and value kinds, and nothing else, so
    an unverified answer shape can be checked without logging content.

    Under ``shadow`` the caller must not act on the answers: the result says so, and
    the caller decides.
    """

    purpose: str
    mode: str
    outcome: str
    reason: str | None = None
    answers: Mapping[str, Answer] = field(default_factory=dict)
    dropped: tuple[str, ...] = ()
    shapes: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    elapsed_ms: int = 0
    usage: dict[str, int] | None = None
    model_echoed: str | None = None
    request_bytes: int = 0
    request_sent: bool = False

    @property
    def shadow(self) -> bool:
        return self.mode == "shadow"

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"


def _depth(value: Any, limit: int, level: int = 1) -> int:
    if level > limit:
        raise _Invalid("state nests too deep")
    if isinstance(value, Mapping):
        return max([level, *(_depth(item, limit, level + 1) for item in value.values())])
    if isinstance(value, list):
        return max([level, *(_depth(item, limit, level + 1) for item in value)])
    return level


def _state(state: Any) -> str | dict[str, Any]:
    """The state as sent: text, or a JSON object of finite values."""
    if isinstance(state, str):
        if not state.strip() or len(state) > STATE_LIMIT:
            raise _Invalid("state must be non-empty text within the limit")
        return state
    if isinstance(state, Mapping):
        try:
            text = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            value = json.loads(text)
        except (TypeError, ValueError, RecursionError) as exc:
            raise _Invalid("state must be a JSON object") from exc
        if not value or len(text) > STATE_LIMIT:
            raise _Invalid("state must be a non-empty object within the limit")
        _depth(value, STATE_DEPTH_LIMIT)
        return value
    raise _Invalid("state must be text or an object")


def _questions(questions: Any) -> dict[str, Question]:
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise _Invalid(f"questions must map 1..{MAX_QUESTIONS} names to questions")
    for name, question in questions.items():
        if not isinstance(name, str) or NAME.fullmatch(name) is None:
            raise _Invalid("a question name must be a lowercase identifier")
        if not isinstance(question, QUESTION_TYPES):
            raise _Invalid("each question must be built by imprint.decide")
    return dict(questions)


def request_body(model: str, state: Any, questions: Mapping[str, Question]) -> dict[str, object]:
    """The exact request body. Raises ``ValueError`` when anything is invalid."""
    try:
        if not isinstance(model, str) or _MODEL_NAME.fullmatch(model) is None:
            raise _Invalid("model must be a model name")
        named = _questions(questions)
        return {"model": model, "state": _state(state), "questions": {n: q.request() for n, q in named.items()}}
    except _Invalid as exc:
        raise ValueError(str(exc)) from None


def encode_request(model: str, state: Any, questions: Mapping[str, Question]) -> bytes:
    """The exact bytes ``ask`` would send. Raises ``ValueError`` when invalid or too large."""
    encoded = json.dumps(
        request_body(model, state, questions), ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    return encoded


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "other"


def _shape(answer: Any) -> dict[str, str]:
    """Field names and value kinds of one raw answer; never a value."""
    if not isinstance(answer, dict):
        return {"": _kind(answer)}
    names = sorted(name for name in answer if isinstance(name, str) and _FIELD.fullmatch(name))
    return {name: _kind(answer[name]) for name in names[:_MAX_SHAPE_FIELDS]}


# -- the daily cap ---------------------------------------------------------------

def counter_path(root: Path) -> Path:
    return Path(root) / "logs" / COUNTER_NAME


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` where the platform has ``flock``."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        try:
            import fcntl
        except ImportError:  # Windows: calls from one process at a time are assumed
            fcntl = None
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read_counter(path: Path, day: str) -> dict[str, int]:
    """Today's counts. Raises on anything it cannot trust, so the cap fails closed."""
    try:
        status = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_COUNTER_BYTES:
        raise ValueError("counter file is not a small regular file")
    with path.open("rb") as handle:
        value = json.loads(handle.read(MAX_COUNTER_BYTES + 1))
    if not isinstance(value, dict) or not isinstance(value.get("day"), str) or not isinstance(value.get("calls"), dict):
        raise ValueError("counter file has the wrong shape")
    calls = value["calls"]
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in calls.values()):
        raise ValueError("counter file has a bad count")
    return dict(calls) if value["day"] == day else {}


def _write_counter(path: Path, day: str, calls: Mapping[str, int]) -> None:
    data = json.dumps({"calls": dict(sorted(calls.items())), "day": day}, separators=(",", ":")).encode("ascii")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".imprint-decide-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        secure_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def calls_today(root: Path, purpose: str, *, day: str | None = None) -> int | None:
    """Calls ``purpose`` has made today, or None when the counter cannot be read."""
    try:
        return _read_counter(counter_path(root), day or _today()).get(purpose, 0)
    except Exception:
        return None


def _reserve(root: Path, purpose: str, cap: int, day: str) -> str | None:
    """Take one call from today's cap. None when taken, else the closed reason."""
    if cap <= 0:
        return "cap_reached"
    try:
        path = counter_path(root)
        if not path.parent.is_dir():
            secure_directory(path.parent)
        with _locked(path.parent / COUNTER_LOCK_NAME):
            calls = _read_counter(path, day)
            if calls.get(purpose, 0) >= cap:
                return "cap_reached"
            calls[purpose] = calls.get(purpose, 0) + 1
            _write_counter(path, day, calls)
        return None
    except Exception:
        return "cap_unavailable"


# -- the decision log --------------------------------------------------------------

def decision_log_path(root: Path, purpose: str) -> Path:
    return Path(root) / "logs" / f"decide-{purpose}.jsonl"


def decision_entry(result: DecisionResult, questions: Any) -> dict[str, object]:
    """One content-free line: names, types, numbers and closed codes. Never the
    state, an instruction, a criterion or a key."""
    named: dict[str, str] = {}
    if isinstance(questions, Mapping):
        for name, question in list(questions.items())[:MAX_QUESTIONS]:
            if isinstance(name, str) and NAME.fullmatch(name) and isinstance(question, QUESTION_TYPES):
                named[name] = question.type
    entry: dict[str, object] = {
        "answers": {name: {"type": answer.type, **answer.values()} for name, answer in result.answers.items()},
        "dropped": list(result.dropped),
        "elapsed_ms": result.elapsed_ms,
        "mode": result.mode,
        "model_echoed": result.model_echoed,
        "outcome": result.outcome,
        "purpose": result.purpose,
        "questions": named,
        "reason": result.reason,
        "request_bytes": result.request_bytes,
        "request_sent": result.request_sent,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "usage": result.usage,
    }
    shapes = {name: dict(result.shapes[name]) for name in result.dropped if name in result.shapes}
    if shapes:
        entry["dropped_shapes"] = shapes
    return entry


def _log(root: Path, result: DecisionResult, questions: Any) -> None:
    try:
        append_capped(decision_log_path(root, result.purpose), decision_entry(result, questions), DECISION_LOG_MAX_BYTES)
    except Exception:
        pass


# -- the call --------------------------------------------------------------------

def _settings(config: Mapping[str, Any]) -> JevSettings:
    settings = JevSettings.from_config(config["selector"]["jev"])
    parts = urlsplit(settings.endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _Invalid("endpoint must be an https URL without credentials")
    return settings


def ask(
    purpose: str,
    state: Any,
    questions: Mapping[str, Question],
    *,
    config: Mapping[str, Any],
    root: Path,
    transport: Callable[[str, bytes, Mapping[str, str], float], bytes] | None = None,
    clock: Callable[[], float] = time.monotonic,
    model: str | None = None,
    timeout_seconds: float | None = None,
    day: str | None = None,
) -> DecisionResult:
    """Ask Jev ``questions`` about ``state`` for ``purpose``. Never raises.

    ``config`` is a loaded Imprint config and ``root`` the operator root, whose
    ``logs`` folder holds the daily counter and the decision logs. ``model`` and
    ``timeout_seconds`` default to ``selector.jev``'s. ``day`` (UTC, ``YYYY-MM-DD``)
    is for tests.
    """
    started = clock()
    try:
        mode, cap, off_reason = purpose_mode(config, purpose)
    except Exception:
        mode, cap, off_reason = "off", 0, "purpose_off"
    name = purpose if isinstance(purpose, str) and off_reason != "unknown_purpose" else "unknown"
    if mode == "off":
        return DecisionResult(purpose=name, mode="off", outcome="disabled", reason=off_reason)

    request_bytes = 0
    request_sent = False

    def finish(outcome: str, reason: str | None = None, **values: Any) -> DecisionResult:
        elapsed = max(0, int(round((clock() - started) * 1000)))
        result = DecisionResult(
            purpose=name, mode=mode, outcome=outcome, reason=reason, elapsed_ms=elapsed,
            request_bytes=request_bytes, request_sent=request_sent, **values,
        )
        _log(root, result, questions)
        return result

    try:
        settings = _settings(config)
        chosen_model = settings.model if model is None else model
        timeout = settings.timeout_seconds if timeout_seconds is None else timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise _Invalid("timeout must be above 0 and at most 30 seconds")
        if day is not None and (not isinstance(day, str) or _DAY.fullmatch(day) is None):
            raise _Invalid("day must be YYYY-MM-DD")
        try:
            encoded = encode_request(chosen_model, state, questions)
        except ValueError as exc:
            raise _Invalid(str(exc)) from None
        named = dict(questions)
        deadline = started + float(timeout)
        try:
            key = resolve_api_key(settings)
        except _KeyUnavailable as exc:
            return finish("fallback", exc.reason)
        refused = _reserve(root, name, cap, day or _today())
        if refused is not None:
            return finish("capped", refused)
        remaining = deadline - clock()
        if remaining <= 0:
            return finish("fallback", "timeout")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": f"imprint-local/{__version__}",
        }
        send = transport or _jev.http_post  # looked up per call, so a test's fake applies
        request_bytes = len(encoded)
        request_sent = True
        raw = _run_with_deadline(lambda: send(settings.endpoint, encoded, headers, remaining), remaining)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError, AttributeError) as exc:
            raise _BadResponse("response is not JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("answers"), dict):
            raise _BadResponse("response answers must be an object")
        replies = value["answers"]
        answers: dict[str, Answer] = {}
        shapes: dict[str, dict[str, str]] = {}
        dropped: list[str] = []
        for question_name, question in named.items():
            reply = replies.get(question_name)
            if question_name in replies:
                shapes[question_name] = _shape(reply)
            try:
                parsed = question.parse(reply) if reply is not None else None
            except Exception:
                parsed = None  # one answer that cannot be read never costs the others
            if parsed is None:
                dropped.append(question_name)
            else:
                answers[question_name] = parsed
        if not answers:
            return finish(
                "fallback", "bad_response", dropped=tuple(dropped), shapes=shapes,
                usage=_usage(value.get("usage")), model_echoed=_echoed_model(value.get("model")),
            )
        return finish(
            "answered", None, answers=answers, dropped=tuple(dropped), shapes=shapes,
            usage=_usage(value.get("usage")), model_echoed=_echoed_model(value.get("model")),
        )
    except _Invalid:
        return finish("fallback", "invalid_request")
    except _DeadlineExceeded:
        return finish("fallback", "timeout")
    except _BadResponse:
        return finish("fallback", "bad_response")
    except Exception as exc:
        try:
            reason = _failure_reason(exc)
        except Exception:
            reason = "exception"
        try:
            return finish("fallback", reason)
        except Exception:
            return DecisionResult(purpose=name, mode=mode, outcome="fallback", reason="exception")


__all__ = [
    "COUNTER_NAME",
    "DecisionResult",
    "MAX_QUESTIONS",
    "MAX_REQUEST_BYTES",
    "OUTCOMES",
    "REASONS",
    "STATE_LIMIT",
    "ask",
    "calls_today",
    "counter_path",
    "decision_entry",
    "decision_log_path",
    "encode_request",
    "request_body",
]
