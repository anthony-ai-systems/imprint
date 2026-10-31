"""Stop capture stays honest about turns it cannot keep, and never blocks on them.

Exit 2 forces the model to continue, so it is only for a failure a retry can heal.
Each case here fails the same way on a retry, so it must skip or capture instead.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from imprint.capture import transcript as transcript_module
from imprint.capture.schema import _MAX_TEXT
from imprint.capture.transcript import (
    MAX_CONTEXT_BYTES,
    MAX_TRANSCRIPT_BYTES,
    TranscriptUnavailable,
    _message_text,
    _read_native_transcript_snapshot,
    parse_native_stop_transcript,
)
from imprint.cli import _parse_large_native_transcript
from imprint.errors import ValidationError

REPO = Path(__file__).resolve().parents[2]
FEEDBACK = "No, keep the source label in the summary."
TYPED = {"type": "user", "promptSource": "typed", "uuid": "user-1"}


def _user(text=FEEDBACK, **extra):
    return {**TYPED, "message": {"content": text}, **extra}


def _assistant(text="Draft."):
    return {"type": "assistant", "message": {"content": text}}


def _write(tmp_path, *entries, tail=""):
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries) + tail, encoding="utf-8")
    return path


def _padded(tmp_path, *entries):
    """The same entries past the full parser's limit, so Stop reads the tail."""
    path = tmp_path / "transcript.jsonl"
    padding = (json.dumps({"type": "system", "text": "x" * 4096}) + "\n").encode("utf-8")
    body = "".join(json.dumps(entry) + "\n" for entry in entries).encode("utf-8")
    path.write_bytes(padding * (MAX_TRANSCRIPT_BYTES // len(padding) + 1) + body)
    return path


def _stop(tmp_path, path, **event):
    config = tmp_path / "config.json"
    data = tmp_path / "data"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "test",
    }), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "IMPRINT_CAPTURE_ORIGIN"}
    env.update(IMPRINT_CONFIG=str(config), PYTHONPATH=str(REPO / "src"))
    event = {"hook_event_name": "Stop", "session_id": "session-synthetic", "transcript_path": str(path), **event}
    run = subprocess.run(
        [sys.executable, "-m", "imprint.cli", "hook", "stop-capture"],
        input=json.dumps(event), text=True, capture_output=True, env=env, check=False,
    )
    return run, data / "test"


def _spooled(root):
    return list((root / "spool").rglob("*.json"))


def _envelope(root):
    (path,) = _spooled(root)
    return json.loads(path.read_text(encoding="utf-8"))


def _skips(root):
    path = root / "logs" / "capture-skips.jsonl"
    return [json.loads(line)["reason"] for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("build", [_write, _padded], ids=["full", "tail"])
def test_a_correction_after_a_very_long_assistant_message_is_captured(tmp_path, build):
    run, root = _stop(tmp_path, build(tmp_path, _assistant("y" * 300_000), _user()))
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["status"] == "queued"
    context = [item for item in _envelope(root)["evidence"] if item["kind"] == "context"]
    assert len(context[0]["content"].encode("utf-8")) == MAX_CONTEXT_BYTES


def test_a_shortened_context_is_recorded_on_the_capture(tmp_path):
    run, root = _stop(tmp_path, _write(tmp_path, _assistant("y" * 300_000), _user()))
    receipt = json.loads(run.stdout)
    assert receipt["degradation"]["context_truncated"] is True
    assert receipt["degradation"]["truncated"] is False
    assert receipt["degradation"]["hash_scope"] == "complete_transcript"
    payload = _envelope(root)["extensions"]["org.imprint.transcript"]["payload"]
    assert payload["receipt"] == "long_context_truncated"
    assert "y" * 100 not in json.dumps(payload)


def test_a_context_within_the_limit_records_no_degradation(tmp_path):
    run, root = _stop(tmp_path, _write(tmp_path, _assistant("y" * MAX_CONTEXT_BYTES), _user()))
    assert "degradation" not in json.loads(run.stdout)
    assert "org.imprint.transcript" not in _envelope(root)["extensions"]


def test_truncating_a_long_context_never_splits_a_character(tmp_path):
    parsed = parse_native_stop_transcript(str(_write(tmp_path, _assistant("é" * 200_000), _user())))
    assert set(parsed["prior_assistant_output"]) == {"é"}
    assert len(parsed["prior_assistant_output"].encode("utf-8")) <= MAX_CONTEXT_BYTES


@pytest.mark.parametrize("build", [_write, _padded], ids=["full", "tail"])
def test_operator_text_over_the_limit_is_skipped_and_never_cut(tmp_path, build):
    text = "No, keep the source label. " + "z" * _MAX_TEXT
    run, root = _stop(tmp_path, build(tmp_path, _assistant(), _user(text)))
    assert (run.returncode, run.stderr) == (0, "")
    assert json.loads(run.stdout)["reason"] == "operator_text_oversized"
    assert not _spooled(root)
    assert _skips(root) == ["operator_text_oversized"]
    assert "z" * 10 not in (root / "logs" / "capture-skips.jsonl").read_text(encoding="utf-8")


def test_operator_text_at_the_limit_is_captured_whole(tmp_path):
    text = ("No, keep the source label. " + "z" * _MAX_TEXT)[:_MAX_TEXT]
    run, root = _stop(tmp_path, _write(tmp_path, _assistant(), _user(text)))
    assert json.loads(run.stdout)["status"] == "queued"
    assert _envelope(root)["verdict"]["raw_operator_text"] == text


def test_an_oversized_turn_that_is_not_feedback_is_an_ordinary_skip(tmp_path):
    run, root = _stop(tmp_path, _write(tmp_path, _assistant(), _user("z" * (_MAX_TEXT + 1))))
    assert json.loads(run.stdout)["reason"] == "not_explicit_feedback"
    assert not (root / "logs" / "capture-skips.jsonl").exists()


def test_an_oversized_turn_claims_no_mark(tmp_path):
    text = "No, keep the source label. " + "z" * _MAX_TEXT
    _, root = _stop(tmp_path, _write(tmp_path, _assistant(), _user(text)))
    assert not list((root / "runtime" / "captured-turns").rglob("*.json"))


def test_an_oversized_direct_event_is_skipped_too(tmp_path):
    text = "No, keep the source label. " + "z" * _MAX_TEXT
    run, root = _stop(tmp_path, tmp_path / "unused.jsonl", operator_text=text)
    assert (run.returncode, json.loads(run.stdout)["reason"]) == (0, "operator_text_oversized")


def test_an_empty_transcript_is_skipped_and_leaves_a_trace(tmp_path):
    run, root = _stop(tmp_path, _write(tmp_path))
    assert (run.returncode, run.stderr) == (0, "")
    assert json.loads(run.stdout)["reason"] == "transcript_unavailable"
    assert _skips(root) == ["transcript_unavailable"]


def test_an_empty_transcript_is_unavailable_not_refused(tmp_path):
    with pytest.raises(TranscriptUnavailable):
        _read_native_transcript_snapshot(str(_write(tmp_path)))


def test_a_file_that_disappears_during_the_read_is_unavailable(tmp_path, monkeypatch):
    path = _write(tmp_path, _user())
    real_read = transcript_module._read_native_transcript_snapshot_once
    attempts = []

    def vanishing(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            path.unlink()
            raise transcript_module._TranscriptChangedDuringRead
        return real_read(*args, **kwargs)

    monkeypatch.setattr(transcript_module, "_read_native_transcript_snapshot_once", vanishing)
    with pytest.raises(TranscriptUnavailable):
        _read_native_transcript_snapshot(str(path))
    assert len(attempts) == 2


def test_a_file_removed_between_stat_and_open_is_unavailable(tmp_path, monkeypatch):
    path = _write(tmp_path, _user())
    real_open = os.open

    def removed_first(target, *args, **kwargs):
        if Path(target) == path:
            path.unlink()
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(transcript_module.os, "open", removed_first)
    with pytest.raises(TranscriptUnavailable):
        _read_native_transcript_snapshot(str(path))


def test_a_file_replaced_during_the_read_is_still_refused(tmp_path, monkeypatch):
    path = _write(tmp_path, _user())

    def changed(*args, **kwargs):
        raise transcript_module._TranscriptChangedDuringRead

    monkeypatch.setattr(transcript_module, "_read_native_transcript_snapshot_once", changed)
    with pytest.raises(ValidationError, match="changed during the bounded read") as refused:
        _read_native_transcript_snapshot(str(path))
    assert not isinstance(refused.value, TranscriptUnavailable)


@pytest.mark.parametrize("tail", ["\n", "   \n", "\r\n", "\n\n", "  "], ids=repr)
@pytest.mark.parametrize("parser", [parse_native_stop_transcript, _parse_large_native_transcript])
def test_blank_lines_are_skipped_by_both_parsers(parser, tmp_path, tail):
    path = _write(tmp_path, _assistant(), _user(), tail=tail)
    assert parser(str(path))["operator_text"] == FEEDBACK


def test_a_blank_line_between_entries_is_skipped(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps(_assistant()) + "\n\n   \n" + json.dumps(_user()) + "\n", encoding="utf-8")
    assert parse_native_stop_transcript(str(path))["operator_text"] == FEEDBACK


@pytest.mark.parametrize("parser", [parse_native_stop_transcript, _parse_large_native_transcript])
@pytest.mark.parametrize("line, message", [
    ('{"type": "user"\n{"type": "user"}\n', "malformed complete transcript line 1"),
    ('{"type": "user"}\n{"type"', "incomplete transcript line 2"),
])
def test_a_malformed_line_is_still_refused_by_both_parsers(parser, tmp_path, line, message):
    path = tmp_path / "transcript.jsonl"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ValidationError, match=message):
        parser(str(path))


def test_lone_surrogates_are_replaced_in_message_text():
    assert _message_text("Draft \ud83d end") == "Draft ? end"
    assert _message_text([{"type": "text", "text": "Draft \ud83d end"}]) == "Draft ? end"
    assert _message_text("Draft \U0001F600 end") == "Draft \U0001F600 end"


@pytest.mark.parametrize("build", [_write, _padded], ids=["full", "tail"])
def test_a_lone_surrogate_in_the_assistant_text_does_not_lose_the_capture(tmp_path, build):
    run, root = _stop(tmp_path, build(tmp_path, _assistant("Draft \ud83d end"), _user()))
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["status"] == "queued"
    assert len(_spooled(root)) == 1


@pytest.mark.parametrize("build", [_write, _padded], ids=["full", "tail"])
def test_a_lone_surrogate_in_the_operator_text_does_not_lose_the_capture(tmp_path, build):
    run, root = _stop(tmp_path, build(tmp_path, _assistant(), _user("No, keep \ud83d the label.")))
    assert run.returncode == 0, run.stdout + run.stderr
    assert _envelope(root)["verdict"]["raw_operator_text"] == "No, keep ? the label."


def test_a_lone_surrogate_in_the_entry_id_does_not_lose_the_capture(tmp_path):
    run, root = _stop(tmp_path, _write(tmp_path, _assistant(), _user(uuid="entry-\ud83d")))
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(_spooled(root)) == 1


@pytest.mark.parametrize("field", ["operator_text", "session_id", "reason"])
def test_a_lone_surrogate_in_the_stop_event_does_not_crash_the_hook(tmp_path, field):
    event = {"operator_text": FEEDBACK, field: "No, keep the label \ud83d here."}
    run, root = _stop(tmp_path, tmp_path / "unused.jsonl", **event)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["status"] == "queued"


@pytest.mark.parametrize("build", [_write, _padded], ids=["full", "tail"])
def test_a_line_too_deep_to_parse_is_skipped(tmp_path, build):
    deep = "[" * 200_000 + "]" * 200_000
    path = build(tmp_path, _assistant(), _user())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(deep + "\n")
    run, root = _stop(tmp_path, path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["status"] == "queued"


@pytest.mark.parametrize("line", ["[" * 200_000 + "]" * 200_000, "1" * 5000], ids=["deep", "long integer"])
def test_a_line_too_large_to_parse_does_not_touch_a_later_turn(tmp_path, line):
    path = tmp_path / "transcript.jsonl"
    path.write_text(line + "\n" + json.dumps(_user("hello there")) + "\n", encoding="utf-8")
    run, _ = _stop(tmp_path, path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["reason"] == "not_explicit_feedback"


@pytest.mark.parametrize("parser", [parse_native_stop_transcript, _parse_large_native_transcript])
def test_a_long_integer_line_is_skipped_by_both_parsers(parser, tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps(_user()) + "\n" + "1" * 5000 + "\n", encoding="utf-8")
    assert parser(str(path))["operator_text"] == FEEDBACK


def test_a_symlink_is_still_refused_after_the_unavailable_changes(tmp_path):
    target = _write(tmp_path, _user())
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    run, root = _stop(tmp_path, link)
    assert run.returncode != 0
    assert not (root / "logs" / "capture-skips.jsonl").exists()
