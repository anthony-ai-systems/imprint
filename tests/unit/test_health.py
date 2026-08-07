import json

import pytest

from imprint.health import HealthInputs, evaluate_health


def healthy(**changes):
    values = dict(compiler_count=1, database_ok=True, migrations_ok=True)
    values.update(changes)
    return evaluate_health(HealthInputs(**values))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"compiler_count": 0}, "compiler_missing"),
        ({"compiler_count": 2}, "compiler_duplicate"),
        ({"database_ok": False}, "database_integrity_failed"),
        ({"migrations_ok": False}, "migration_invalid"),
        ({"config_ok": False}, "config_invalid"),
        ({"backend_ok": False}, "required_backend_unavailable"),
        ({"spool_depth": 1, "oldest_spool_age_seconds": 3601}, "spool_stale"),
        ({"selected_bytes": 33000}, "retrieval_budget_violated"),
        ({"record_schema_major": 4}, "record_schema_unsupported"),
        ({"hook_schema_major": 2}, "hook_schema_unsupported"),
        ({"projection_snapshot_present": False}, "projection_snapshot_missing"),
        ({"stale_lock_count": 1}, "stale_lock_present"),
        ({"abandoned_temp_count": 1}, "abandoned_temp_present"),
        ({"backup_verified": False}, "backup_unverified"),
        ({"hook_failure_count": 1}, "hook_failures_present"),
        ({"experimental_enabled": True, "experimental_state": "stalled"}, "experimental_loop_stalled"),
    ],
)
def test_required_failures_turn_health_red(changes, reason):
    report = healthy(**changes)
    assert report.status == "red" and report.exit_code == 1
    assert reason in report.degraded_reasons


def test_recent_activity_is_not_a_health_input_and_output_is_content_free():
    report = healthy(compiler_count=0, spool_depth=7, quarantine_count=2)
    encoded = json.dumps(report.as_dict(), sort_keys=True)
    assert "operator secret sentence" not in encoded
    assert set(report.as_dict()) == {"health_schema_version", "status", "degraded_reasons", "metrics"}
    assert "compiler_missing" in report.degraded_reasons


def test_hook_failure_backlog_is_visible_as_counts_and_ages_only():
    report = healthy(hook_failure_count=117, hook_failure_newest_age_seconds=345600)
    assert report.status == "red"
    assert "hook_failures_present" in report.degraded_reasons
    assert report.metrics["hook_failure_count"] == 117
    assert report.metrics["hook_failure_newest_age_seconds"] == 345600
    assert report.metrics["hook_failure_evidence"] == "operator_logs_hook_failures_file_scan"


def test_absent_hook_failure_backlog_is_healthy_and_reports_unknown_age():
    report = healthy()
    assert report.status == "green"
    assert "hook_failures_present" not in report.degraded_reasons
    assert report.metrics["hook_failure_count"] == 0
    assert report.metrics["hook_failure_newest_age_seconds"] == -1


def test_explicit_higher_budget_is_visible_and_allowed():
    report = healthy(retrieval_budget_bytes=40 * 1024, higher_budget_explicit=True)
    assert report.status == "green"
    assert report.metrics["higher_budget_explicit"] is True
    assert healthy(retrieval_budget_bytes=40 * 1024).status == "red"
