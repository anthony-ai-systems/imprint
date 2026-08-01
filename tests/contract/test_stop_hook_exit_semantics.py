"""Stop-hook bridge exit semantics toward the Claude Code host.

Exit 2 from a Stop hook blocks the host's stop and feeds stderr (not stdout)
back to the model. The bridge must therefore:

  - exit 2 only when the capture subprocess definitely failed (the one error
    a blocked stop can heal), with the reason on stderr
  - never exit 2 when stop_hook_active is true -- the host is already
    continuing because of a previous block, and re-blocking loops forever
  - fail open (exit 0) on invalid input, a missing or hung executable, and
    malformed output: none of those are healed by retrying, so blocking on
    them wedges the host in a stop loop whenever the install is degraded

These are exercised hermetically: the bridge module is loaded in-process and
its `_graceful_run` / `sys.stdin` are monkeypatched, so no real capture
subprocess is spawned. That keeps the semantics independent of PYTHONPATH,
HOME, and whether imprint is pip-installed -- the definite-capture-failure
class is modeled by a stub returning a non-zero returncode.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from datetime import datetime

import pytest

from pathlib import Path

ROOT = Path(__file__).parents[2]


def _bridge_module():
    spec = importlib.util.spec_from_file_location(
        "imprint_test_bridge_exit_semantics", ROOT / "hooks" / "_bridge.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_failed(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    # The definite-capture-failure class: the CLI ran but reported failure
    # (returncode != 0). This is the one error a blocked stop can heal.
    return subprocess.CompletedProcess(args[0] if args else [], 1, "", "boom")


def test_definite_capture_failure_blocks_once_with_reason_on_stderr(monkeypatch, capsys):
    bridge = _bridge_module()
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": False})))
    monkeypatch.setattr(bridge, "_graceful_run", _capture_failed)

    assert bridge.run("stop-capture") == 2
    captured = capsys.readouterr()
    assert "judgment capture failed" in captured.err
    body = json.loads(captured.out)
    assert body["error"] == "hook_action_failed"
    assert body["failure_policy"] == "fail_closed"


def test_definite_capture_failure_persists_subprocess_output(monkeypatch, capsys, tmp_path):
    bridge = _bridge_module()
    config = tmp_path / "config.json"
    data_root = tmp_path / "data"
    config.write_text(json.dumps({
        "data_root": str(data_root), "operator_slug": "test-operator",
    }))
    monkeypatch.setenv("IMPRINT_CONFIG", str(config))
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": False})))
    monkeypatch.setattr(
        bridge,
        "_graceful_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0] if args else [], 7, "captured stdout", "captured stderr",
        ),
    )

    assert bridge.run("stop-capture") == 2
    capsys.readouterr()
    records = list((data_root / "test-operator" / "logs" / "hook-failures").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).tzinfo is not None
    assert record["action"] == "stop-capture"
    assert record["exit_code"] == 7
    assert record["stdout"] == "captured stdout"
    assert record["stderr"] == "captured stderr"


def test_stop_hook_active_suppresses_reblocking(monkeypatch, capsys):
    bridge = _bridge_module()
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": True})))
    monkeypatch.setattr(bridge, "_graceful_run", _capture_failed)

    assert bridge.run("stop-capture") == 0
    assert json.loads(capsys.readouterr().out)["failure_policy"] == "fail_open"


def test_invalid_input_fails_open_instead_of_blocking(monkeypatch, capsys):
    bridge = _bridge_module()
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO("not json"))
    # Invalid input is rejected before any capture subprocess is spawned.
    monkeypatch.setattr(
        bridge, "_graceful_run",
        lambda *a, **k: pytest.fail("invalid input must not spawn the capture subprocess"),
    )

    assert bridge.run("stop-capture") == 0
    body = json.loads(capsys.readouterr().out)
    assert body["error"] == "hook_input_invalid"
    assert body["failure_policy"] == "fail_open"


def test_failure_stdout_is_a_single_valid_json_object(monkeypatch, capsys):
    """The host parses stdout as JSON on exit 0; mixed text breaks it."""
    for stdin_text in (json.dumps({"stop_hook_active": True}), "not json"):
        bridge = _bridge_module()
        monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(stdin_text))
        monkeypatch.setattr(bridge, "_graceful_run", _capture_failed)
        bridge.run("stop-capture")
        json.loads(capsys.readouterr().out)


def test_hung_child_gets_sigterm_grace_before_kill(monkeypatch, tmp_path):
    """A timed-out child is asked to unwind (SIGTERM) before being killed.

    A hard kill mid-write orphans WAL sidecars; the grace window lets the CLI
    close the store cleanly. The child here models that: its SIGTERM handler
    leaves a marker, standing in for a clean store close.
    """
    bridge = _bridge_module()
    monkeypatch.setattr(bridge, "HOOK_TIMEOUT_SECONDS", 0.3)
    marker = tmp_path / "unwound-cleanly"
    script = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM,"
        f" lambda *a: (open({str(marker)!r}, 'w').close(), sys.exit(0)))\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        bridge._graceful_run([sys.executable, "-c", script], "")
    assert marker.exists()
