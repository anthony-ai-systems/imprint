from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import imprint.capture.transcript as transcript_module
from imprint.capture.transcript import MAX_TRANSCRIPT_BYTES, parse_native_stop_transcript
from imprint.cli import _parse_large_native_transcript
from imprint.errors import ValidationError


def _write_transcript(
    path: Path, *, large: bool,
    operator_text: str = "No, preserve the descriptor-bound source.",
) -> None:
    payload = (
        json.dumps({
            "type": "assistant",
            "message": {"content": "Original assistant context."},
        })
        + "\n"
        + json.dumps({
            "type": "user",
            "promptSource": "typed",
            "message": {"content": operator_text},
        })
        + "\n"
    ).encode()
    with path.open("wb") as handle:
        if large:
            handle.seek(MAX_TRANSCRIPT_BYTES + 1024 - len(payload))
            handle.write(b"\n" + payload)
        else:
            handle.write(payload)


def _parse(path: Path, *, large: bool) -> dict:
    if large:
        return _parse_large_native_transcript(str(path))
    return parse_native_stop_transcript(str(path))


@pytest.mark.parametrize("large", [False, True])
def test_transcript_accepts_append_growth_with_snapshot_anchored_content(
    tmp_path, monkeypatch, large,
):
    path = tmp_path / "transcript.jsonl"
    _write_transcript(path, large=large)
    original_read = os.read
    raced = False
    appended_text = "No, this appended turn is outside the accepted snapshot."

    def grow_then_read(descriptor: int, count: int) -> bytes:
        nonlocal raced
        if not raced:
            raced = True
            with path.open("ab") as handle:
                handle.write((json.dumps({
                    "type": "user", "message": {"content": appended_text},
                }) + "\n").encode())
        return original_read(descriptor, count)

    monkeypatch.setattr(transcript_module.os, "read", grow_then_read)
    parsed = _parse(path, large=large)
    assert parsed["operator_text"] == "No, preserve the descriptor-bound source."
    with path.open("rb") as handle:
        handle.seek(-min(path.stat().st_size, 1024), os.SEEK_END)
        assert appended_text.encode() in handle.read()


@pytest.mark.parametrize("large", [False, True])
def test_transcript_retries_once_after_path_swap_and_uses_stable_replacement(
    tmp_path, monkeypatch, large,
):
    path = tmp_path / "transcript.jsonl"
    held = tmp_path / "held-original.jsonl"
    _write_transcript(path, large=large)
    original_read = os.read
    raced = False

    def swap_then_read(descriptor: int, count: int) -> bytes:
        nonlocal raced
        if not raced:
            raced = True
            path.rename(held)
            _write_transcript(
                path, large=large,
                operator_text="No, use the stable replacement after rotation.",
            )
        return original_read(descriptor, count)

    monkeypatch.setattr(transcript_module.os, "read", swap_then_read)
    parsed = _parse(path, large=large)
    assert parsed["operator_text"] == "No, use the stable replacement after rotation."


@pytest.mark.parametrize("large", [False, True])
def test_transcript_rejects_persistent_truncation_after_one_retry(
    tmp_path, monkeypatch, large,
):
    path = tmp_path / "transcript.jsonl"
    _write_transcript(path, large=large)
    original_open = os.open
    original_read = os.read
    generations: dict[int, int] = {}
    mutated: set[int] = set()
    opened = 0

    def tracked_open(path_value, flags):
        nonlocal opened
        descriptor = original_open(path_value, flags)
        opened += 1
        generations[descriptor] = opened
        return descriptor

    def truncate_then_read(descriptor: int, count: int) -> bytes:
        generation = generations[descriptor]
        if generation not in mutated:
            mutated.add(generation)
            with path.open("r+b") as handle:
                handle.truncate(os.fstat(descriptor).st_size - 1)
        return original_read(descriptor, count)

    monkeypatch.setattr(transcript_module.os, "open", tracked_open)
    monkeypatch.setattr(transcript_module.os, "read", truncate_then_read)
    with pytest.raises(ValidationError, match="changed during the bounded read"):
        _parse(path, large=large)
    assert mutated == {1, 2}


@pytest.mark.parametrize("large", [False, True])
def test_transcript_rejects_symlink_before_open(tmp_path, large):
    target = tmp_path / "target.jsonl"
    link = tmp_path / "transcript.jsonl"
    _write_transcript(target, large=large)
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(
        ValidationError,
        match="absolute regular non-symlink file",
    ):
        _parse(link, large=large)
