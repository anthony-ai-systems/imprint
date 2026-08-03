"""Bounded parsing of native Claude Code transcript JSONL hook input."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imprint.errors import ValidationError

from .provenance import (
    NO_OPERATOR_MESSAGE_REASON,
    SYNTHETIC_ENTRY_REASON,
    classify_entry_provenance,
)

MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class _TranscriptSnapshot:
    data: bytes
    size: int
    offset: int


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


class _TranscriptChangedDuringRead(Exception):
    pass


def _read_native_transcript_snapshot_once(
    path_value: str, *, tail_limit: int | None = None,
) -> _TranscriptSnapshot:
    """Open once and return a bounded, immutable view of a regular file.

    The path checks, size decision, and reads are bound to the same descriptor.
    Append-only growth is accepted because reads stop at the snapshot size;
    rotation or truncation is reported to the bounded retry wrapper.
    """
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise ValidationError("transcript_path must be an absolute regular non-symlink file")
    try:
        path_before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValidationError("transcript_path must be an absolute regular non-symlink file") from exc
    if not stat.S_ISREG(path_before.st_mode):
        raise ValidationError("transcript_path must be an absolute regular non-symlink file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValidationError("transcript_path must be an absolute regular non-symlink file") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(path_before):
            raise ValidationError("transcript_path changed while it was being opened")
        size = opened.st_size
        if size <= 0:
            raise ValidationError("transcript_path size is outside the supported bound")
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
        except ValidationError as exc:
            if changed is None:
                raise
            changed = exc
    raise ValidationError("transcript_path changed during the bounded read") from changed


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _entry_identity(item: dict[str, Any]) -> str | None:
    value = item.get("uuid") or item.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_native_stop_snapshot(snapshot: _TranscriptSnapshot) -> dict[str, str | None]:
    messages: list[tuple[str, str, str | None]] = []
    synthetic_basis: str | None = None
    try:
        lines = snapshot.data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise ValidationError("transcript is not valid UTF-8") from exc
    for number, raw_line in enumerate(lines, start=1):
        complete = raw_line.endswith(("\n", "\r"))
        raw = raw_line.rstrip("\r\n")
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            if not complete and number == len(lines):
                raise ValidationError(f"incomplete transcript line {number}") from exc
            raise ValidationError(f"malformed complete transcript line {number}") from exc
        if not isinstance(item, dict) or item.get("type") not in {"user", "assistant"}:
            continue
        message = item.get("message", {})
        if not isinstance(message, dict):
            continue
        text = _message_text(message.get("content"))
        if item["type"] == "user":
            verdict = classify_entry_provenance(item, text)
            if not verdict.is_operator:
                synthetic_basis = verdict.basis
                continue
        if not text.strip():
            continue
        messages.append((item["type"], text, _entry_identity(item)))
    locator = "transcript:sha256:" + hashlib.sha256(snapshot.data).hexdigest()
    user_indexes = [index for index, (kind, _, _) in enumerate(messages) if kind == "user"]
    if not user_indexes:
        # No operator turn is a skip, never an error: raising here exits 2 and
        # blocks the host session's stop.
        return {
            "operator_text": None,
            "prior_assistant_output": None,
            "case_description": None,
            "source_locator": locator,
            "skip_reason": SYNTHETIC_ENTRY_REASON if synthetic_basis else NO_OPERATOR_MESSAGE_REASON,
            "provenance_basis": synthetic_basis,
            "source_entry_id": None,
        }
    user_index = user_indexes[-1]
    operator_text = messages[user_index][1]
    prior_assistant = next(
        (messages[index][1] for index in range(user_index - 1, -1, -1) if messages[index][0] == "assistant"),
        None,
    )
    return {
        "operator_text": operator_text,
        "prior_assistant_output": prior_assistant,
        "case_description": "Explicit operator feedback witnessed in the Claude Code transcript",
        "source_locator": locator,
        "skip_reason": None,
        "provenance_basis": None,
        "source_entry_id": messages[user_index][2],
    }


def parse_native_stop_transcript(path_value: str) -> dict[str, str | None]:
    return _parse_native_stop_snapshot(
        _read_native_transcript_snapshot(path_value),
    )
