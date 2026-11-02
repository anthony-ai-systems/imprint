from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

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


def test_hook_failure_backlog_degrades_health_with_content_free_evidence(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    failures = root / "logs" / "hook-failures"
    failures.mkdir(parents=True)
    for index in range(3):
        path = failures / f"2026080{index}T000000.000000Z-{index:032x}.json"
        path.write_text(json.dumps({"stderr": "operator secret sentence"}), encoding="utf-8")
        aged = path.stat().st_mtime - 86400 * (index + 1)
        os.utime(path, (aged, aged))

    report = health_report(root, store, _config(root))

    assert report["status"] == "degraded"
    assert "hook_failures_present" in report["degraded_reasons"]
    assert report["metrics"]["hook_failure_count"] == 3
    assert report["metrics"]["hook_failure_newest_age_seconds"] >= 86000
    assert report["metrics"]["hook_failure_evidence"] == "operator_logs_hook_failures_file_scan"
    encoded = json.dumps(report)
    assert "operator secret sentence" not in encoded
    assert "hook-failures" not in encoded


def _health_with_hook_failures(tmp_path, *ages_in_days):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    failures = root / "logs" / "hook-failures"
    failures.mkdir(parents=True)
    now = datetime.now(timezone.utc).timestamp()
    for number, days in enumerate(ages_in_days):
        failure = failures / f"failure-{number}.json"
        failure.write_text("{}\n", encoding="utf-8")
        os.utime(failure, (now - days * 86400, now - days * 86400))
    return root, failures, health_report(root, store, _config(root))


def test_a_hook_failure_past_seven_days_stops_degrading_and_stays_counted(tmp_path):
    root, failures, report = _health_with_hook_failures(tmp_path, 7.01, 30, 90)
    assert "hook_failures_present" not in report["degraded_reasons"]
    assert report["metrics"]["hook_failure_count"] == 3
    assert report["metrics"]["hook_failure_recent_count"] == 0
    assert report["metrics"]["hook_failure_old_count"] == 3
    assert report["metrics"]["hook_failure_newest_age_seconds"] >= 7 * 86400
    assert len(list(failures.glob("*.json"))) == 3


def test_one_failure_inside_seven_days_still_degrades_over_old_ones(tmp_path):
    _, _, report = _health_with_hook_failures(tmp_path, 6.99, 30, 90)
    assert "hook_failures_present" in report["degraded_reasons"]
    assert report["metrics"]["hook_failure_count"] == 3
    assert report["metrics"]["hook_failure_recent_count"] == 1
    assert report["metrics"]["hook_failure_old_count"] == 2


def test_a_failure_whose_age_cannot_be_read_still_degrades(tmp_path, monkeypatch):
    def unknown_age(path, now):
        return -1

    monkeypatch.setattr(health_module, "_age_seconds", unknown_age)
    _, _, report = _health_with_hook_failures(tmp_path, 30)
    assert "hook_failures_present" in report["degraded_reasons"]
    assert report["metrics"]["hook_failure_recent_count"] == 1


def test_absent_hook_failure_directory_reports_zero_without_degrading(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()

    report = health_report(root, store, _config(root))

    assert not (root / "logs" / "hook-failures").exists()
    assert report["metrics"]["hook_failure_count"] == 0
    assert report["metrics"]["hook_failure_newest_age_seconds"] == -1
    assert "hook_failures_present" not in report["degraded_reasons"]


def test_hook_failure_scan_ignores_symlinks_and_non_json_entries(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    failures = root / "logs" / "hook-failures"
    failures.mkdir(parents=True)
    real = failures / "20260803T000000.000000Z-real.json"
    real.write_text("{}\n", encoding="utf-8")
    (failures / "notes.txt").write_text("ignored\n", encoding="utf-8")
    (failures / "nested.json").mkdir()
    (failures / "linked.json").symlink_to(real)

    report = health_report(root, store, _config(root))

    assert report["metrics"]["hook_failure_count"] == 1
    assert "hook_failures_present" in report["degraded_reasons"]


def _skip_line(reason, age_seconds):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return json.dumps({"reason": reason, "timestamp": stamp.isoformat().replace("+00:00", "Z")}) + "\n"


def _health_with_skip_log(tmp_path, content):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    (root / "logs").mkdir()
    (root / "logs" / "capture-skips.jsonl").write_text(content, encoding="utf-8")
    return health_report(root, store, _config(root))


def test_recent_t3_record_failure_degrades_health_as_counts_and_ages_only(tmp_path):
    report = _health_with_skip_log(
        tmp_path, _skip_line("t3_record_unavailable", 120) + _skip_line("t3_record_unavailable", 600),
    )
    assert report["status"] == "degraded"
    assert "t3_record_unavailable" in report["degraded_reasons"]
    assert report["metrics"]["t3_record_unavailable_24h"] == 2
    assert 120 <= report["metrics"]["t3_record_unavailable_newest_age_seconds"] < 600
    assert report["metrics"]["t3_record_evidence"] == "operator_logs_capture_skips_tail"


def test_old_t3_record_failure_stops_degrading_but_keeps_its_age(tmp_path):
    report = _health_with_skip_log(tmp_path, _skip_line("t3_record_unavailable", 2 * 86400))
    assert "t3_record_unavailable" not in report["degraded_reasons"]
    assert report["metrics"]["t3_record_unavailable_24h"] == 0
    assert report["metrics"]["t3_record_unavailable_newest_age_seconds"] >= 2 * 86400


def test_other_skips_and_damaged_lines_are_not_t3_record_failures(tmp_path):
    content = (
        _skip_line("transcript_unavailable", 60) + "{not json\n[]\n"
        + '{"reason": "t3_record_unavailable"}\n{"reason": "t3_record_unavailable", "timestamp": 7}\n'
        + '{"reason": "t3_record_unavailable", "timestamp": "not-a-time"}\n'
    )
    report = _health_with_skip_log(tmp_path, content)
    assert "t3_record_unavailable" not in report["degraded_reasons"]
    assert report["metrics"]["t3_record_unavailable_24h"] == 0
    assert report["metrics"]["t3_record_unavailable_newest_age_seconds"] == -1


def test_absent_skip_log_reports_zero_and_unknown_age(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    report = health_report(root, store, _config(root))
    assert report["metrics"]["t3_record_unavailable_24h"] == 0
    assert report["metrics"]["t3_record_unavailable_newest_age_seconds"] == -1


def test_skip_log_read_is_bounded_to_its_tail(tmp_path):
    report = _health_with_skip_log(tmp_path, _skip_line("t3_record_unavailable", 60) * 4000)
    assert 0 < report["metrics"]["t3_record_unavailable_24h"] < 4000


def test_symlinked_skip_log_is_not_followed(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_skip_line("t3_record_unavailable", 60), encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "capture-skips.jsonl").symlink_to(outside)
    report = health_report(root, store, _config(root))
    assert report["metrics"]["t3_record_unavailable_24h"] == 0


def test_skip_log_that_is_not_a_regular_file_is_not_read(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    (root / "logs").mkdir()
    os.mkfifo(root / "logs" / "capture-skips.jsonl")
    report = health_report(root, store, _config(root))
    assert report["metrics"]["t3_record_unavailable_24h"] == 0


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


@pytest.mark.parametrize("reason", [
    "t3_marked_window", "t3_session_ref_unreadable", "transcript_malformed", "transcript_refused",
])
def test_a_skip_that_means_capture_has_stopped_degrades_health(tmp_path, reason):
    report = _health_with_skip_log(tmp_path, _skip_line(reason, 60))
    assert reason in report["degraded_reasons"]
    assert report["metrics"]["capture_skips_24h"][reason] == 1


@pytest.mark.parametrize("reason", [
    "operator_text_oversized", "t3_prompt_unmatched", "t3_scratch_thread", "t3_session_unmatched",
    "transcript_tail_exhausted", "transcript_unavailable",
])
def test_a_skip_normal_use_reaches_is_counted_without_degrading_health(tmp_path, reason):
    report = _health_with_skip_log(tmp_path, _skip_line(reason, 60) * 3)
    assert reason not in report["degraded_reasons"]
    assert report["metrics"]["capture_skips_24h"][reason] == 3


def test_an_old_skip_is_not_counted(tmp_path):
    report = _health_with_skip_log(tmp_path, _skip_line("transcript_refused", 2 * 86400))
    assert report["metrics"]["capture_skips_24h"]["transcript_refused"] == 0
    assert "transcript_refused" not in report["degraded_reasons"]
