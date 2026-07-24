from __future__ import annotations

from contextlib import contextmanager
import json

import imprint.health as health_module
from imprint.constants import STORE_SCHEMA_VERSION
from imprint.errors import ValidationError
from imprint.health import health_report
from imprint.store import ImprintStore


def _config(root):
    hooks = root / "hooks"
    hooks.mkdir(parents=True)
    for name in ("session_start.py", "user_prompt_submit.py", "stop_capture.py", "health_check.py"):
        (hooks / name).write_text("# test\n")
    return {
        "compiler": True, "context_budget_bytes": 32768,
        "experimental": {"digest": False, "profile_learning": False},
        "hooks_dir": str(hooks),
    }


def test_health_reports_fresh_bare_lock_as_invalid_but_not_stale(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    (root / "compiler.lock").mkdir()
    report = health_report(root, store, _config(root))
    assert report["status"] == "degraded"
    assert report["metrics"]["stale_lock_count"] == 0
    assert report["metrics"]["compiler_state"] == "invalid"
    assert "compiler_lock_invalid" in report["degraded_reasons"]
    assert "stale_lock_present" not in report["degraded_reasons"]
    encoded = json.dumps(report)
    assert str(root) not in encoded


def test_health_does_not_claim_projection_or_backup_that_do_not_exist(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    report = health_report(root, store, _config(root))
    assert report["metrics"]["projection_snapshot_present"] is False
    assert report["metrics"]["backup_verified"] is False


def test_sidecar_contention_is_busy_and_does_not_degrade_database_or_migrations(
    tmp_path, monkeypatch,
):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.path.write_bytes(b"occupied")
    attempts = []

    @contextmanager
    def contended():
        attempts.append(True)
        raise ValidationError(health_module._SIDECAR_PREFLIGHT_ERROR)
        yield

    monkeypatch.setattr(store, "connect", contended)
    monkeypatch.setattr(health_module.time, "sleep", lambda delay: None)

    report = health_report(root, store, _config(root))

    assert len(attempts) == 3
    assert report["metrics"]["database_state"] == "busy"
    assert report["metrics"]["database_ok"] is False
    assert report["metrics"]["migrations_ok"] is False
    assert "database_integrity_failed" not in report["degraded_reasons"]
    assert "migration_invalid" not in report["degraded_reasons"]


def test_definite_integrity_failure_after_connection_still_degrades(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    path = root / "imprint.db"
    path.write_bytes(b"synthetic")

    class Result:
        def __init__(self, *, one=None, all_rows=()):
            self.one = one
            self.all_rows = all_rows

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.all_rows

    class Connection:
        def execute(self, statement):
            if statement == "PRAGMA integrity_check":
                return Result(one=("damaged",))
            if "store_schema_version" in statement:
                return Result(one=(STORE_SCHEMA_VERSION,))
            if "consumed_inputs" in statement:
                return Result(all_rows=())
            raise AssertionError(statement)

    class Store:
        def __init__(self, store_path):
            self.path = store_path

        @contextmanager
        def connect(self):
            yield Connection()

    report = health_report(root, Store(path), _config(root))

    assert report["metrics"]["database_state"] == "failed"
    assert "database_integrity_failed" in report["degraded_reasons"]
    assert "migration_invalid" not in report["degraded_reasons"]
