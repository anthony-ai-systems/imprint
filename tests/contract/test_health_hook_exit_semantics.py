"""Health reports are informational SessionStart context, including degradation."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _bridge_module():
    spec = importlib.util.spec_from_file_location(
        "imprint_test_health_bridge", ROOT / "hooks" / "_bridge.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_degraded_health_exit_two_is_passed_through_without_failure_record(
    monkeypatch, capsys,
):
    bridge = _bridge_module()
    child = {
        "hook_schema_version": "1.0.0",
        "health_schema_version": "1.0.0",
        "status": "degraded",
        "degraded_reasons": ["database_integrity_failed", "spool_stale"],
        "metrics": {"database_ok": False},
    }
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps({})))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 2, json.dumps(child), "",
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_persist_failure",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("valid degraded health must not persist a failure")
        ),
    )

    assert bridge.run("health-check") == 0
    body = json.loads(capsys.readouterr().out)
    assert {key: body[key] for key in child} == child
    assert body["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": (
            "imprint health: degraded — database_integrity_failed, spool_stale"
        ),
    }


def test_health_child_crash_keeps_generic_failure_behavior(monkeypatch, capsys):
    bridge = _bridge_module()
    persisted = []
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps({})))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7, "", "boom"),
    )
    monkeypatch.setattr(bridge, "_persist_failure", lambda action, process: persisted.append((action, process)))

    assert bridge.run("health-check") == 0
    body = json.loads(capsys.readouterr().out)
    assert body["error"] == "hook_action_failed"
    assert body["failure_policy"] == "fail_open"
    assert len(persisted) == 1
