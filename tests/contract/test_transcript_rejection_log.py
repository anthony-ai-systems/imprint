"""A refused or malformed transcript says which check turned it away, never its path.

Every path here carries a marker, and no assertion passes if the log holds it.
"""
import errno
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from imprint import cli
from imprint.capture.transcript import (
    MAX_TRANSCRIPT_BYTES,
    _read_native_transcript_snapshot,
    parse_native_stop_transcript,
)
from imprint.cli import _parse_large_native_transcript
from imprint.errors import ValidationError
from imprint.health import health_report
from imprint.store import ImprintStore

REPO = Path(__file__).resolve().parents[2]
MARKER = "synthetic-private-folder"
REFUSED_MESSAGE = "transcript_path must be an absolute regular non-symlink file"
USER = json.dumps({"type": "user", "promptSource": "typed", "message": {"content": "No, keep the label."}})


def _config(tmp_path, data="data"):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(tmp_path / data), "operator_slug": "test", "node_id": "test",
    }), encoding="utf-8")
    return config, tmp_path / data / "test"


def _stop(tmp_path, path, data="data"):
    config, root = _config(tmp_path, data)
    env = {**os.environ, "IMPRINT_CONFIG": str(config), "PYTHONPATH": str(REPO / "src")}
    env.pop("IMPRINT_CAPTURE_ORIGIN", None)
    event = {"hook_event_name": "Stop", "session_id": "session-synthetic", "transcript_path": str(path)}
    run = subprocess.run(
        [sys.executable, str(REPO / "hooks" / "stop_capture.py")],
        input=json.dumps(event), text=True, capture_output=True, env=env, check=False,
    )
    assert (run.returncode, run.stderr) == (0, ""), run.stdout + run.stderr
    return json.loads(run.stdout), root


def _log(root):
    return (root / "logs" / "capture-skips.jsonl").read_text(encoding="utf-8")


def _lines(root):
    return [json.loads(line) for line in _log(root).splitlines()]


def _private(tmp_path):
    folder = tmp_path / MARKER
    folder.mkdir(exist_ok=True)
    return folder


def _symlink(tmp_path):
    target = _private(tmp_path) / "target.jsonl"
    target.write_text(USER + "\n", encoding="utf-8")
    link = _private(tmp_path) / "link.jsonl"
    link.symlink_to(target)
    return link


def _fifo(tmp_path):
    path = _private(tmp_path) / "pipe.jsonl"
    os.mkfifo(path)
    return path


def _directory(tmp_path):
    path = _private(tmp_path) / "folder.jsonl"
    path.mkdir()
    return path


def _through_a_file(tmp_path):
    parent = _private(tmp_path) / "plain-file"
    parent.write_text("", encoding="utf-8")
    return parent / "transcript.jsonl"


@pytest.mark.parametrize("build, code, path_class, error", [
    (lambda tmp_path: f"relative/{MARKER}/transcript.jsonl", "path_not_absolute", "relative", None),
    (lambda tmp_path: "", "path_not_absolute", "empty", None),
    (lambda tmp_path: "/" + MARKER * 500, "path_stat_failed", "absolute", "ENAMETOOLONG"),
    (_through_a_file, "path_stat_failed", "absolute", "ENOTDIR"),
    (lambda tmp_path: f"/{MARKER}/with\u0000null.jsonl", "path_stat_failed", "null_byte", None),
    (lambda tmp_path: f"~no-such-user-{MARKER}/transcript.jsonl", "path_stat_failed", "home_relative", None),
    (_symlink, "path_not_regular", "symlink", None),
    (_directory, "path_not_regular", "directory", None),
    (_fifo, "path_not_regular", "fifo", None),
], ids=[
    "relative", "empty", "too long", "through a file", "null byte", "unknown home",
    "symlink", "directory", "fifo",
])
def test_a_refused_path_logs_its_site_and_no_path(tmp_path, build, code, path_class, error):
    receipt, root = _stop(tmp_path, build(tmp_path))
    assert receipt == {"hook_schema_version": "1.0.0", "status": "skipped", "reason": "transcript_refused"}
    assert MARKER not in _log(root)
    assert str(tmp_path) not in _log(root)
    (line,) = _lines(root)
    assert line["reason"] == "transcript_refused"
    assert line["code"] == code
    assert line["path_class"] == path_class
    assert line.get("errno") == error
    assert re.fullmatch(r"[0-9a-f]{16}", line["path_hash"])
    expected = {"code", "path_class", "path_hash", "reason", "timestamp"} | ({"errno"} if error else set())
    assert set(line) == expected
    assert not (root / "logs" / "hook-failures").exists()


def _in_process_stop(tmp_path, path, monkeypatch, capsys):
    config, root = _config(tmp_path)
    event = {"hook_event_name": "Stop", "session_id": "session-synthetic", "transcript_path": str(path)}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    assert cli.main(["--config", str(config), "hook", "stop-capture"]) == 0
    return json.loads(capsys.readouterr().out), root


@pytest.mark.parametrize("number, name", [(errno.ELOOP, "ELOOP"), (errno.EACCES, "EACCES")])
def test_a_file_that_passes_stat_and_fails_to_open_logs_the_fourth_site(tmp_path, monkeypatch, capsys, number, name):
    path = _private(tmp_path) / "transcript.jsonl"
    path.write_text(USER + "\n", encoding="utf-8")
    real_open = os.open

    def refusing_open(target, *args, **kwargs):
        if Path(target) == path:
            raise OSError(number, "synthetic refusal")
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(os, "open", refusing_open)
    receipt, root = _in_process_stop(tmp_path, path, monkeypatch, capsys)
    assert receipt["reason"] == "transcript_refused"
    (line,) = _lines(root)
    assert (line["code"], line["errno"], line["path_class"]) == ("path_open_failed", name, "absolute")
    assert MARKER not in _log(root)


def test_an_errno_with_no_name_is_still_logged(tmp_path, monkeypatch, capsys):
    path = _private(tmp_path) / "transcript.jsonl"
    path.write_text(USER + "\n", encoding="utf-8")
    real_open = os.open

    def refusing_open(target, *args, **kwargs):
        if Path(target) == path:
            raise OSError(1_000_000, "synthetic refusal")
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(os, "open", refusing_open)
    _, root = _in_process_stop(tmp_path, path, monkeypatch, capsys)
    assert _lines(root)[0]["errno"] == "1000000"


@pytest.mark.parametrize("value", ["relative/transcript.jsonl", "/x\u0000y", "/" + "x" * 5000])
def test_the_four_sites_keep_the_one_message_and_no_path_in_the_detail(value):
    with pytest.raises(ValidationError) as caught:
        _read_native_transcript_snapshot(value)
    assert str(caught.value) == REFUSED_MESSAGE
    assert value not in json.dumps(caught.value.detail)


def test_the_hash_groups_repeats_of_one_path_and_parts_two(tmp_path):
    first, second = _symlink(tmp_path), _directory(tmp_path)
    for path in (first, first, second):
        _, root = _stop(tmp_path, path)
    hashes = [line["path_hash"] for line in _lines(root)]
    assert hashes[0] == hashes[1] != hashes[2]


def test_the_hash_is_keyed_so_a_guessed_path_cannot_be_checked(tmp_path):
    path = _symlink(tmp_path)
    _, root = _stop(tmp_path, path)
    _, other = _stop(tmp_path, path, data="other-install")
    (line,), (elsewhere,) = _lines(root), _lines(other)
    assert line["path_hash"] != elsewhere["path_hash"]
    assert line["path_hash"] not in hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _torn(path):
    path.write_text('{"type":"assistant","mess{"type":"user"}\n' + USER + "\n", encoding="utf-8")
    return path


def _not_utf8(path):
    path.write_bytes(b'{"type":"assistant","message":{"content":"\xff"}}\n' + USER.encode("utf-8") + b"\n")
    return path


def _padded(path):
    """Push the same bad line past the full parser's limit, so Stop reads the tail."""
    body = path.read_bytes()
    padding = (json.dumps({"type": "system", "text": "x" * 4096}) + "\n").encode("utf-8")
    path.write_bytes(padding * (MAX_TRANSCRIPT_BYTES // len(padding) + 1) + body)
    return path


@pytest.mark.parametrize("damage, code", [(_torn, "malformed_line"), (_not_utf8, "not_utf8")], ids=["torn", "not utf-8"])
@pytest.mark.parametrize("reach", [lambda path: path, _padded], ids=["full", "tail"])
def test_a_malformed_transcript_is_named_apart_from_a_refused_file(tmp_path, damage, code, reach):
    path = reach(damage(_private(tmp_path) / "transcript.jsonl"))
    receipt, root = _stop(tmp_path, path)
    assert receipt == {"hook_schema_version": "1.0.0", "status": "skipped", "reason": "transcript_malformed"}
    (line,) = _lines(root)
    assert set(line) == {"code", "path_hash", "reason", "timestamp"}
    assert (line["reason"], line["code"]) == ("transcript_malformed", code)
    assert MARKER not in _log(root)
    assert not (root / "logs" / "hook-failures").exists()


@pytest.mark.parametrize("parser", [parse_native_stop_transcript, _parse_large_native_transcript])
def test_both_parsers_keep_the_malformed_line_message(parser, tmp_path):
    with pytest.raises(ValidationError, match="malformed complete transcript line 1"):
        parser(str(_torn(tmp_path / "transcript.jsonl")))


def test_refused_files_and_malformed_lines_count_apart_in_health(tmp_path):
    bad_line = _torn(_private(tmp_path) / "transcript.jsonl")
    _stop(tmp_path, bad_line)
    _stop(tmp_path, bad_line)
    _, root = _stop(tmp_path, _symlink(tmp_path))
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("session_start.py", "user_prompt_submit.py", "stop_capture.py", "health_check.py"):
        (hooks / name).write_text("# test\n")
    report = health_report(root, store, {
        "compiler": True, "context_budget_bytes": 32768, "hooks_dir": str(hooks),
        "experimental": {"digest": False, "profile_learning": False},
    })
    counts = report["metrics"]["capture_skips_24h"]
    assert (counts["transcript_malformed"], counts["transcript_refused"]) == (2, 1)
    assert {"transcript_malformed", "transcript_refused"} <= set(report["degraded_reasons"])
    assert MARKER not in json.dumps(report)


def test_a_transcript_the_host_has_not_written_stays_a_bare_line(tmp_path):
    _, root = _stop(tmp_path, tmp_path / MARKER / "absent.jsonl")
    (line,) = _lines(root)
    assert set(line) == {"reason", "timestamp"}
    assert line["reason"] == "transcript_unavailable"
