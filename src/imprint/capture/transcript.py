"""Bounded parsing of native Claude Code transcript JSONL hook input."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imprint.errors import ValidationError

from .provenance import (
    MID_TURN_SOURCE,
    UNVERIFIED_BASIS,
    HostOrigin,
    classify_with_host,
    skip_reason,
)

MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
# How much of the prior assistant message is kept as context for a capture.
MAX_CONTEXT_BYTES = 64 * 1024
# How far back a huge transcript is read at first, and at most. A long turn can
# push the prompt it answers out of the first read.
TAIL_READ_BYTES = 2 * 1024 * 1024
TRANSCRIPT_UNAVAILABLE_REASON = "transcript_unavailable"
TRANSCRIPT_REFUSED_REASON = "transcript_refused"
TRANSCRIPT_MALFORMED_REASON = "transcript_malformed"
TRANSCRIPT_TAIL_EXHAUSTED_REASON = "transcript_tail_exhausted"
# The four refusal sites share one message, so these codes are what tells a repeat
# apart in the skip log. They are stable: anything reading that log may match them.
PATH_NOT_ABSOLUTE = "path_not_absolute"
PATH_STAT_FAILED = "path_stat_failed"
PATH_NOT_REGULAR = "path_not_regular"
PATH_OPEN_FAILED = "path_open_failed"
MALFORMED_LINE = "malformed_line"
NOT_UTF8 = "not_utf8"
_PATH_REFUSED_MESSAGE = "transcript_path must be an absolute regular non-symlink file"
_FILE_CLASSES = (
    (stat.S_ISLNK, "symlink"),
    (stat.S_ISDIR, "directory"),
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISBLK, "block_device"),
    (stat.S_ISCHR, "char_device"),
)


@dataclass(frozen=True)
class _TranscriptSnapshot:
    data: bytes
    size: int
    offset: int


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


class _TranscriptChangedDuringRead(Exception):
    pass


class TranscriptUnavailable(ValidationError):
    """The host named a transcript with nothing in it to read yet, or none at all."""


class TranscriptInFlux(ValidationError):
    """The host is still writing or replacing the transcript, so a retry can heal this."""


class TranscriptRejected(ValidationError):
    """A transcript no retry can read, with content-free detail for the skip log."""

    def __init__(self, message: str, code: str, **detail: str) -> None:
        super().__init__(message)
        self.detail = {"code": code, **detail}


class TranscriptRefused(TranscriptRejected):
    """A path that cannot be read as a transcript. Nothing here holds the path itself."""

    def __init__(self, code: str, path_class: str, cause: Exception | None = None) -> None:
        number = getattr(cause, "errno", None)
        detail = {} if number is None else {"errno": errno.errorcode.get(number, str(number))}
        super().__init__(_PATH_REFUSED_MESSAGE, code, path_class=path_class, **detail)


class TranscriptMalformed(TranscriptRejected):
    """A file that was read and holds a line that cannot be trusted."""


def _path_class(value: str) -> str:
    """Name the shape of a path value without any of its text."""
    if not value:
        return "empty"
    if "\x00" in value:
        return "null_byte"
    if value.startswith("~"):
        return "home_relative"
    return "absolute" if os.path.isabs(value) else "relative"


def _file_class(mode: int) -> str:
    return next((name for matches, name in _FILE_CLASSES if matches(mode)), "other")


def _read_native_transcript_snapshot_once(
    path_value: str, *, tail_limit: int | None = None,
) -> _TranscriptSnapshot:
    """Open once and return a bounded, immutable view of a regular file.

    The path checks, size decision, and reads are bound to the same descriptor.
    Append-only growth is accepted because reads stop at the snapshot size;
    rotation or truncation is reported to the bounded retry wrapper.
    """
    try:
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            raise TranscriptRefused(PATH_NOT_ABSOLUTE, _path_class(path_value))
        path_before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise TranscriptUnavailable("transcript_path does not exist") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise TranscriptRefused(PATH_STAT_FAILED, _path_class(path_value), exc) from exc
    if not stat.S_ISREG(path_before.st_mode):
        raise TranscriptRefused(PATH_NOT_REGULAR, _file_class(path_before.st_mode))

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise TranscriptUnavailable("transcript_path does not exist") from exc
    except OSError as exc:
        raise TranscriptRefused(PATH_OPEN_FAILED, _path_class(path_value), exc) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(path_before):
            raise TranscriptInFlux("transcript_path changed while it was being opened")
        size = opened.st_size
        if size <= 0:
            raise TranscriptUnavailable("transcript_path is empty")
        if size > MAX_TRANSCRIPT_BYTES and tail_limit is None:
            raise ValidationError("transcript_path size is outside the supported bound")

        read_size = size if size <= MAX_TRANSCRIPT_BYTES else min(size, tail_limit or 0)
        offset = size - read_size
        os.lseek(descriptor, offset, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = read_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise _TranscriptChangedDuringRead from exc
        if (
            remaining
            or not stat.S_ISREG(after.st_mode)
            or _identity(after) != _identity(opened)
            or after.st_size < size
            or not stat.S_ISREG(path_after.st_mode)
            or _identity(path_after) != _identity(opened)
            or path_after.st_size < size
        ):
            raise _TranscriptChangedDuringRead
        return _TranscriptSnapshot(data=b"".join(chunks), size=size, offset=offset)
    finally:
        os.close(descriptor)


def _read_native_transcript_snapshot(
    path_value: str, *, tail_limit: int | None = None,
) -> _TranscriptSnapshot:
    """Return a stable snapshot, retrying one rotation or truncation."""

    changed: Exception | None = None
    for _ in range(2):
        try:
            return _read_native_transcript_snapshot_once(
                path_value, tail_limit=tail_limit,
            )
        except _TranscriptChangedDuringRead as exc:
            changed = exc
        except TranscriptUnavailable:
            raise
        except ValidationError as exc:
            if changed is None:
                raise
            changed = exc
    raise TranscriptInFlux("transcript_path changed during the bounded read") from changed


def _well_formed(value: str) -> str:
    """Replace lone surrogates, which JSON can carry and nothing else can encode."""
    return value.encode("utf-8", "replace").decode("utf-8")


def _truncate_utf8(value: str, byte_limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value, False
    return encoded[:byte_limit].decode("utf-8", errors="ignore"), True


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return _well_formed(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return _well_formed("\n".join(parts))
    return ""


def _entry_identity(item: dict[str, Any]) -> str | None:
    value = item.get("uuid") or item.get("id")
    return _well_formed(value.strip()) if isinstance(value, str) and value.strip() else None


def _queued_prompt(item: dict[str, Any]) -> dict[str, Any] | None:
    """Return a prompt queued into a running turn as the user entry it stands for.

    Claude records a mid-turn prompt as an attachment, not a user entry, and with
    no transport of its own. What it declares about itself still counts: a machine
    origin, a meta flag or a forwarded intent is not a person steering the turn.
    """
    attachment = item.get("attachment")
    if (
        item.get("type") != "attachment"
        or not isinstance(attachment, dict)
        or attachment.get("type") != "queued_command"
        or attachment.get("commandMode") != "prompt"
        or attachment.get("forwardedIntent") is not None
    ):
        return None
    return {
        **item,
        "type": "user",
        "uuid": attachment.get("source_uuid") or item.get("uuid"),
        "origin": attachment.get("origin") or item.get("origin"),
        "isMeta": bool(attachment.get("isMeta") or item.get("isMeta")),
        "promptSource": MID_TURN_SOURCE,
        "message": {"content": attachment.get("prompt")},
    }


def _transcript_entry(item: Any, host_origin: HostOrigin | None) -> dict[str, Any] | None:
    """Return a parsed line as an entry both parsers read, or None to pass it over.

    A mid-turn prompt counts only where a host record can judge it. Without one it
    could only ever be unverified, and that would hide the turn it interrupted.
    """
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if isinstance(kind, str) and kind in {"user", "assistant"}:
        return item
    return _queued_prompt(item) if host_origin is not None else None


def collect_turns(
    lines: list[str], host_origin: HostOrigin | None,
) -> tuple[list[tuple[str, str, str | None, str | None, Any]], str | None, bool]:
    """Read decoded transcript lines into turns, the same way for both parsers.

    Returns the turns, the basis of the last dropped entry, and whether a prompt
    nobody can vouch for is newer than the last operator entry.
    """
    messages: list[tuple[str, str, str | None, str | None, Any]] = []
    synthetic_basis: str | None = None
    unverified_since_operator = False
    for number, raw_line in enumerate(lines, start=1):
        complete = raw_line.endswith(("\n", "\r"))
        raw = raw_line.rstrip("\r\n")
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            if not complete and number == len(lines):
                raise TranscriptInFlux(f"incomplete transcript line {number}") from exc
            raise TranscriptMalformed(f"malformed complete transcript line {number}", MALFORMED_LINE) from exc
        except (ValueError, RecursionError):
            continue  # Valid JSON too deep or too long to read says nothing about this turn.
        entry = _transcript_entry(item, host_origin)
        if entry is None:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = _message_text(message.get("content"))
        basis = None
        if entry["type"] == "user":
            verdict = classify_with_host(entry, text, host_origin)
            if not verdict.is_operator:
                synthetic_basis = verdict.basis
                unverified_since_operator |= verdict.basis == UNVERIFIED_BASIS
                continue
            basis = verdict.basis
        if not text.strip():
            continue
        if entry["type"] == "user":
            unverified_since_operator = False
        messages.append((entry["type"], text, _entry_identity(entry), basis, entry.get("timestamp")))
    return messages, synthetic_basis, unverified_since_operator


def _parse_native_stop_snapshot(
    snapshot: _TranscriptSnapshot, *, host_origin: HostOrigin | None = None,
) -> dict[str, Any]:
    try:
        # Split before decoding: str.splitlines also breaks on U+2028, U+2029
        # and U+0085, which are legal raw inside a JSON string.
        lines = [line.decode("utf-8") for line in snapshot.data.splitlines(keepends=True)]
    except UnicodeDecodeError as exc:
        raise TranscriptMalformed("transcript is not valid UTF-8", NOT_UTF8) from exc
    messages, synthetic_basis, unverified_since_operator = collect_turns(lines, host_origin)
    evidence_sha256 = hashlib.sha256(snapshot.data).hexdigest()
    locator = f"transcript:sha256:{evidence_sha256}"
    user_indexes = [index for index, message in enumerate(messages) if message[0] == "user"]
    if not user_indexes or unverified_since_operator:
        # No operator turn is a skip, never an error: raising here exits 2 and
        # blocks the host session's stop. A newer prompt nobody can vouch for
        # ends the search too: an older turn is not what this Stop answered.
        basis = UNVERIFIED_BASIS if unverified_since_operator else synthetic_basis
        return {
            "operator_text": None,
            "prior_assistant_output": None,
            "case_description": None,
            "source_locator": locator,
            "skip_reason": skip_reason(basis),
            "provenance_basis": basis,
            "source_entry_id": None,
        }
    user_index = user_indexes[-1]
    operator_text = messages[user_index][1]
    prior_assistant = next(
        (messages[index][1] for index in range(user_index - 1, -1, -1) if messages[index][0] == "assistant"),
        None,
    )
    context_truncated = False
    if prior_assistant is not None:
        prior_assistant, context_truncated = _truncate_utf8(prior_assistant, MAX_CONTEXT_BYTES)
    parsed: dict[str, Any] = {
        "operator_text": operator_text,
        "prior_assistant_output": prior_assistant,
        "case_description": "Explicit operator feedback witnessed in the Claude Code transcript",
        "source_locator": locator,
        "skip_reason": None,
        "provenance_basis": messages[user_index][3],
        "source_entry_id": messages[user_index][2],
        "source_timestamp": messages[user_index][4],
    }
    if context_truncated:
        parsed["degradation"] = {
            "schema_version": "1.0.0",
            "payload": {
                "transcript_bytes": snapshot.size,
                "tail_bytes_examined": snapshot.size,
                "evidence_sha256": evidence_sha256,
                "hash_scope": "complete_transcript",
                "truncated": False,
                "context_truncated": True,
                "receipt": "long_context_truncated",
            },
        }
    return parsed


def last_assistant_text(path_value: str, *, tail_limit: int = 2 * 1024 * 1024) -> str | None:
    """Return the newest main-thread assistant text in a native transcript.

    Reuses the descriptor-bound snapshot reader, so the read is bounded by
    ``MAX_TRANSCRIPT_BYTES`` (or ``tail_limit`` above it). The walk starts at
    the end and tolerates partial or malformed lines: callers use this only
    for optional context, never as captured evidence.
    """
    snapshot = _read_native_transcript_snapshot(path_value, tail_limit=tail_limit)
    lines = snapshot.data.split(b"\n")
    if snapshot.offset:
        lines = lines[1:]  # a tail read may start mid-line
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(item, dict) or item.get("type") != "assistant" or item.get("isSidechain") is True:
            continue
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        text = _message_text(message.get("content"))
        if text.strip():
            return text
    return None


def parse_native_stop_transcript(path_value: str) -> dict[str, Any]:
    return _parse_native_stop_snapshot(
        _read_native_transcript_snapshot(path_value),
    )
