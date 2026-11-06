"""A dead Jev is a fault, and an empty one is not. Every line and key here is synthetic."""

from __future__ import annotations

import copy
import io
import json
import os
import time
from datetime import datetime, timezone

import pytest

import imprint.cli as cli
import imprint.hook_routes as routes
import imprint.retrieve as retrieval
from imprint.config import DEFAULTS
from imprint.health import HealthInputs, evaluate_health, health_report
from imprint.health.report import SELECTOR_FAILURE_LIMIT
from imprint.hook_routes import ROUTE_WINDOW_SECONDS, record_route, route_log_path, route_summary
from imprint.retrieve import jev
from imprint.retrieve.models import RetrievalRecord
from imprint.retrieve.receipts import DeliveryReceipts
from imprint.store import ImprintStore

KEY_ENV = "JEV_TEST_KEY"
KEY_REASON = "selector_key_unavailable"
REPEATED_REASON = "selector_failures_repeated"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def selector_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")


def selector(mode: str = "jev", **jev_values) -> dict:
    value = copy.deepcopy(DEFAULTS["selector"])
    value["mode"] = mode
    value["jev"]["api_key_env"] = KEY_ENV
    value["jev"].update(jev_values)
    return value


def stamp(age: float, now: float = NOW) -> str:
    return datetime.fromtimestamp(now - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(age: float = 60, *, now: float = NOW, **changes) -> dict:
    """One failed selection, ``age`` seconds old, unless ``changes`` say otherwise."""
    return {
        "answered": False, "bytes": 0, "detail": "timeout", "method": "deterministic_fallback", "mode": "jev",
        "outcome": "failed", "route": "user_prompt_submit", "ts": stamp(age, now), **changes,
    }


def reply(age: float = 60, *, now: float = NOW) -> dict:
    return line(age, now=now, answered=True, method="jev", outcome="delivered", detail="picked", bytes=40)


def empty(age: float = 60, *, now: float = NOW) -> dict:
    """A turn that had nothing to ask Jev about."""
    return line(age, now=now, outcome="nothing_relevant", detail="no_candidates")


def in_a_row(root, *entries) -> int:
    for entry in entries:
        record_route(root, entry)
    return route_summary(root, now=NOW)["selector_failures_in_a_row"]


def report_for(tmp_path, *entries, mode="jev", config_selector=None) -> dict:
    root = tmp_path / "operator"
    root.mkdir(exist_ok=True)
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    now = time.time()
    for entry in entries:
        values = dict(entry)
        record_route(root, {**values, "ts": stamp(values.pop("_age", 60), now)})
    return health_report(root, store, {
        **copy.deepcopy(DEFAULTS), "hooks_dir": str(tmp_path), "selector": config_selector or selector(mode),
    })


def ages(*values, **changes) -> list[dict]:
    return [{**line(**changes), "_age": value} for value in values]


# -- the rule ----------------------------------------------------------------------

def test_the_limit_is_three():
    assert SELECTOR_FAILURE_LIMIT == 3


def test_one_timeout_does_not_degrade(tmp_path):
    report = report_for(tmp_path, *ages(60))
    assert REPEATED_REASON not in report["degraded_reasons"]
    assert report["metrics"]["selector_failures_in_a_row"] == 1


def test_one_failure_under_the_limit_does_not_degrade(tmp_path):
    report = report_for(tmp_path, *ages(*range(60, 60 + SELECTOR_FAILURE_LIMIT - 1)))
    assert REPEATED_REASON not in report["degraded_reasons"]


def test_exactly_the_limit_degrades(tmp_path):
    report = report_for(tmp_path, *ages(*range(60, 60 + SELECTOR_FAILURE_LIMIT)))
    assert REPEATED_REASON in report["degraded_reasons"]
    assert report["metrics"]["selector_failures_in_a_row"] == SELECTOR_FAILURE_LIMIT
    assert report["metrics"]["selector_failure_limit"] == SELECTOR_FAILURE_LIMIT


def test_every_kind_of_failed_selection_counts(tmp_path):
    entries = [line(detail=detail) for detail in ("missing_key", "key_file_unsafe", "http_error", "bad_response", "exception")]
    assert in_a_row(tmp_path, *entries) == 5


def test_the_window_edge_is_inside_and_a_second_past_it_is_out(tmp_path):
    root = tmp_path / "edge"
    assert in_a_row(root, line(ROUTE_WINDOW_SECONDS), line(ROUTE_WINDOW_SECONDS), line(ROUTE_WINDOW_SECONDS)) == 3
    root = tmp_path / "past"
    assert in_a_row(root, *[line(ROUTE_WINDOW_SECONDS + 1)] * 3) == 0


def test_the_window_is_a_day():
    assert ROUTE_WINDOW_SECONDS == 86400


def test_failures_that_have_aged_out_stop_degrading(tmp_path):
    report = report_for(tmp_path, *ages(*[ROUTE_WINDOW_SECONDS + 60] * 50))
    assert REPEATED_REASON not in report["degraded_reasons"]
    assert report["metrics"]["selector_failures_in_a_row"] == 0
    assert report["metrics"]["hook_route_outcomes_24h"]["user_prompt_submit"]["failed"] == 0


def test_a_failure_that_ages_out_no_longer_helps_reach_the_limit(tmp_path):
    assert in_a_row(tmp_path, line(ROUTE_WINDOW_SECONDS + 1), line(120), line(60)) == 2


def test_a_reply_between_failures_starts_the_count_again(tmp_path):
    assert in_a_row(tmp_path, line(), line(), reply(), line()) == 1


def test_failures_after_a_reply_still_reach_the_limit(tmp_path):
    report = report_for(tmp_path, *ages(500), reply(400), *ages(300, 200, 100))
    assert REPEATED_REASON in report["degraded_reasons"]


def test_a_reply_clears_the_reason_at_once(tmp_path):
    report = report_for(tmp_path, *ages(300, 200, 100), {**reply(), "_age": 50})
    assert REPEATED_REASON not in report["degraded_reasons"]
    assert report["metrics"]["selector_failures_in_a_row"] == 0


def test_a_reply_with_picks_that_would_not_fit_is_still_a_reply(tmp_path):
    undelivered = line(method="jev", detail="oversized_picks", answered=True)
    assert in_a_row(tmp_path, line(), line(), undelivered) == 0


def test_a_reply_past_the_window_clears_nothing_after_it(tmp_path):
    assert in_a_row(tmp_path, reply(ROUTE_WINDOW_SECONDS + 1), line(), line(), line()) == 3


# -- what never counts -------------------------------------------------------------

def test_empty_turns_are_not_failures(tmp_path):
    report = report_for(tmp_path, *[{**empty(), "_age": 60} for _ in range(50)])
    assert REPEATED_REASON not in report["degraded_reasons"]
    assert report["metrics"]["selector_failures_in_a_row"] == 0
    assert report["metrics"]["hook_route_outcomes_24h"]["user_prompt_submit"]["nothing_relevant"] == 50


def test_a_turn_that_never_asked_neither_adds_to_a_run_nor_ends_it(tmp_path):
    skipped = line(outcome="scratch_skip", detail=None, method="deterministic")
    start = line(outcome="delivered", detail=None, method="deterministic", route="session_start", bytes=40)
    replayed = line(outcome="already_delivered", detail=None, method="jev")
    assert in_a_row(tmp_path, line(), empty(), skipped, start, replayed, line()) == 2


@pytest.mark.parametrize("not_selection", [
    {"detail": "oversized_records"},
    {"detail": "hook_action_timeout", "method": None},
    {"detail": "hook_exception", "method": "deterministic"},
    {"detail": "oversized_picks", "method": "jev"},
    {"detail": "timeout", "method": "shadow"},
], ids=["unfit", "bridge", "crash", "undelivered", "shadow"])
def test_a_failed_turn_that_was_not_a_failed_selection_does_not_count(tmp_path, not_selection):
    assert in_a_row(tmp_path, *[line(**not_selection)] * 5) == 0


def test_lines_from_another_mode_do_not_count(tmp_path):
    assert in_a_row(tmp_path, line(mode="shadow"), line(mode="deterministic"), line(mode="shadow")) == 0


@pytest.mark.parametrize("mode", ["deterministic", "shadow"])
def test_a_store_not_on_jev_never_raises_these_reasons(tmp_path, monkeypatch, mode):
    monkeypatch.delenv(KEY_ENV)
    report = report_for(tmp_path, *ages(*range(60, 70)), mode=mode)
    assert KEY_REASON not in report["degraded_reasons"]
    assert REPEATED_REASON not in report["degraded_reasons"]


def test_the_environment_switch_turns_both_reasons_off(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    monkeypatch.setenv("IMPRINT_SELECTOR", "off")
    report = report_for(tmp_path, *ages(*range(60, 70)))
    assert report["metrics"]["selector_mode"] == "deterministic"
    assert KEY_REASON not in report["degraded_reasons"]
    assert REPEATED_REASON not in report["degraded_reasons"]


# -- the key -----------------------------------------------------------------------

def test_a_missing_key_degrades_at_once_with_no_traffic(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    report = report_for(tmp_path)
    assert KEY_REASON in report["degraded_reasons"]
    assert report["metrics"]["selector_key_present"] is False
    assert not route_log_path(tmp_path / "operator").exists()


def test_a_key_in_the_environment_is_enough(tmp_path):
    assert KEY_REASON not in report_for(tmp_path)["degraded_reasons"]


def test_a_key_file_is_judged_by_its_metadata_and_never_opened(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    key = tmp_path / "synthetic-key"
    key.write_text("not a usable key, which only reading it could show")
    key.chmod(0o600)

    def never(*args, **kwargs):
        raise AssertionError("health must not read the key")

    real_open = os.open

    def opened(path, *args, **kwargs):
        assert str(path) != str(key), "health must not open the key"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(jev, "_read_key_file", never)
    monkeypatch.setattr(jev, "resolve_api_key", never)
    monkeypatch.setattr(os, "open", opened)
    report = report_for(tmp_path, config_selector=selector(api_key_file=str(key)))
    assert KEY_REASON not in report["degraded_reasons"]
    assert "synthetic-key" not in json.dumps(report)


def test_an_environment_value_is_taken_as_set_and_left_unread(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not a usable key")
    assert KEY_REASON not in report_for(tmp_path)["degraded_reasons"]


KEY_FILES = {
    "empty": ("", 0o600),
    "group_readable": ("synthetic", 0o640),
    "no_owner_read": ("synthetic", 0o000),
    "write_only": ("synthetic", 0o200),
    "too_long_to_be_a_key": ("x" * (jev.MAX_KEY_FILE_BYTES + 1), 0o600),
}


@pytest.mark.parametrize("prepare", ["absent", "directory", *KEY_FILES])
def test_a_key_file_that_will_not_serve_degrades(tmp_path, monkeypatch, prepare):
    monkeypatch.delenv(KEY_ENV)
    key = tmp_path / "synthetic-key"
    if prepare == "directory":
        key.mkdir()
    elif prepare != "absent":
        key.write_text(KEY_FILES[prepare][0])
        key.chmod(KEY_FILES[prepare][1])
    report = report_for(tmp_path, config_selector=selector(api_key_file=str(key)))
    assert KEY_REASON in report["degraded_reasons"]
    assert str(key) not in json.dumps(report)


def test_a_key_file_at_the_size_limit_is_present(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    key = tmp_path / "synthetic-key"
    key.write_text("x" * jev.MAX_KEY_FILE_BYTES)
    key.chmod(0o400)
    assert KEY_REASON not in report_for(tmp_path, config_selector=selector(api_key_file=str(key)))["degraded_reasons"]


def test_a_selector_that_cannot_load_is_not_called_a_missing_key(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(jev, "selector_health", boom)
    report = report_for(tmp_path)
    assert KEY_REASON not in report["degraded_reasons"]
    assert report["metrics"]["selector_key_present"] is False


def test_a_damaged_decision_log_cannot_hide_a_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    logs = tmp_path / "operator" / "logs"
    logs.mkdir(parents=True, mode=0o700)
    damaged = {"ts": "0001-01-01T00:00:00+14:00", "outcome": "fallback", "request_sent": True}
    (logs / "jev-selector.jsonl").write_text(json.dumps(damaged) + "\n")
    report = report_for(tmp_path)
    assert KEY_REASON in report["degraded_reasons"]
    assert report["metrics"]["selector_fallbacks_24h"] == 0
    assert report["metrics"]["selector_last_success_ts"] == ""


def test_a_damaged_stamp_never_becomes_the_last_success(tmp_path):
    jev.log_decision(tmp_path, {"ts": "zzz", "outcome": "picked", "request_sent": True})
    summary = jev.decision_log_summary(tmp_path)
    assert summary == {"fallbacks_24h": 0, "last_success_ts": None, "last_transfer_ts": None}


# -- damaged and unreadable logs ---------------------------------------------------

DAMAGED = (
    b"{not json\n", b"[]\n", b"7\n", b"null\n", b"{}\n", b"\xff\xfe\n", b'{"ts": 7, "route": "x"}\n',
    b'{"ts": "not-a-time", "route": "user_prompt_submit", "outcome": "failed"}\n',
    b'{"ts": "9999-99-99T00:00:00Z"}\n',
    b'{"ts": "2026-10-29T11:59:00Z", "route": ["x"], "outcome": "failed"}\n',
    b'{"ts": "2026-10-29T11:59:00Z", "route": "user_prompt_submit", "outcome": ["failed"]}\n',
    b'{"ts": "2026-10-29T11:59:00Z", "route": "user_prompt_submit", "outcome": "a_new_word", "mode": "jev"}\n',
    b'{"ts": "2026-10-29T11:59:00Z", "route": "elsewhere", "outcome": "failed", "mode": "jev"}\n',
    b"[" * 200000 + b"\n",
)


@pytest.mark.parametrize("damage", DAMAGED, ids=range(len(DAMAGED)))
def test_a_damaged_line_counts_as_nothing(tmp_path, damage):
    record_route(tmp_path, line())
    with route_log_path(tmp_path).open("ab") as handle:
        handle.write(damage)
    record_route(tmp_path, line())
    summary = route_summary(tmp_path, now=NOW)
    assert summary["selector_failures_in_a_row"] == 2
    assert summary["outcomes_24h"]["user_prompt_submit"]["failed"] == 2


def test_a_line_torn_by_a_crash_is_skipped(tmp_path):
    record_route(tmp_path, line())
    with route_log_path(tmp_path).open("ab") as handle:
        handle.write(json.dumps(line()).encode()[:40])
    assert route_summary(tmp_path, now=NOW)["selector_failures_in_a_row"] == 1


def test_a_stamp_from_the_future_counts_as_nothing(tmp_path):
    assert in_a_row(tmp_path, *[line(-3600)] * 5) == 0


def test_health_reads_a_damaged_log_without_crashing(tmp_path):
    logs = tmp_path / "operator" / "logs"
    logs.mkdir(parents=True, mode=0o700)
    (logs / "hook-routes.jsonl").write_bytes(b"".join(DAMAGED))
    os.chmod(logs / "hook-routes.jsonl", 0o600)
    report = report_for(tmp_path)
    assert REPEATED_REASON not in report["degraded_reasons"]


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX denial")
def test_a_log_that_cannot_be_read_reports_nothing(tmp_path):
    record_route(tmp_path, line())
    path = route_log_path(tmp_path)
    path.chmod(0o000)
    assert route_summary(tmp_path, now=NOW)["selector_failures_in_a_row"] == 0


def test_a_log_that_is_a_directory_reports_nothing(tmp_path):
    route_log_path(tmp_path).mkdir(parents=True)
    assert route_summary(tmp_path, now=NOW)["selector_failures_in_a_row"] == 0


def test_a_symlinked_log_is_not_followed(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(line()) + "\n")
    path = route_log_path(tmp_path)
    path.parent.mkdir()
    path.symlink_to(outside)
    assert route_summary(tmp_path, now=NOW)["selector_failures_in_a_row"] == 0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_a_log_that_is_a_pipe_is_not_read(tmp_path):
    path = route_log_path(tmp_path)
    path.parent.mkdir()
    os.mkfifo(path)
    assert route_summary(tmp_path, now=NOW)["selector_failures_in_a_row"] == 0


def test_an_absent_log_reports_zero(tmp_path):
    summary = route_summary(tmp_path, now=NOW)
    assert summary["selector_failures_in_a_row"] == 0
    assert set(summary["outcomes_24h"]) == {"session_start", "user_prompt_submit"}
    assert set(summary["outcomes_24h"]["session_start"]) == set(routes.OUTCOMES)


def test_a_run_reads_across_a_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "ROUTE_LOG_MAX_BYTES", 400)
    assert in_a_row(tmp_path, *[line()] * 4) == 4
    assert route_log_path(tmp_path).with_name("hook-routes.jsonl.1").exists()


def test_the_read_is_bounded_to_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "ROUTE_LOG_TAIL_BYTES", 2000)
    assert 0 < in_a_row(tmp_path, *[line()] * 100) < 100


# -- health gains fields and changes nothing else ----------------------------------

BEFORE_THIS_ITEM = {
    "abandoned_temp_count", "backup_evidence", "backup_restoreable", "backup_verified", "capture_skips_24h",
    "compiler_count", "compiler_evidence", "compiler_state", "database_evidence", "database_ok", "database_state",
    "disk_free_bytes", "experimental_state", "higher_budget_explicit", "hook_evidence", "hook_failure_count",
    "hook_failure_evidence", "hook_failure_newest_age_seconds", "hook_failure_old_count", "hook_failure_recent_count",
    "hook_parity_ok", "invalid_backup_count", "last_compile_age_seconds", "last_retrieval_age_seconds",
    "last_transfer_age_seconds", "migration_pending", "migrations_ok", "network_state", "oldest_spool_age_seconds",
    "oldest_unacknowledged_spool_age_seconds", "omitted_bytes", "optional_backend_state", "permissions_evidence",
    "permissions_ok", "projection_snapshot_present", "quarantine_count", "retrieval_budget_bytes",
    "retrieval_omitted_count", "selected_bytes", "selector_endpoint_host", "selector_evidence",
    "selector_fallbacks_24h", "selector_key_present", "selector_last_success_ts", "selector_mode", "spool_depth",
    "spool_evidence", "spool_retention_days", "spool_unacknowledged_count", "stale_lock_count",
    "t3_record_evidence", "t3_record_unavailable_24h", "t3_record_unavailable_newest_age_seconds",
    "unsafe_permission_count", "verified_backup_count",
}
GAINED = {
    "hook_route_evidence", "hook_route_outcomes_24h", "selector_failure_limit", "selector_failures_in_a_row",
}


def test_health_gains_fields_only(tmp_path):
    report = report_for(tmp_path, mode="deterministic")
    assert set(report["metrics"]) == BEFORE_THIS_ITEM | GAINED
    assert set(report) == {"degraded_reasons", "health_schema_version", "metrics", "status"}
    assert report["health_schema_version"] == "1.0.0"
    assert report["metrics"]["hook_route_evidence"] == "operator_logs_hook_routes_tail"


def fallbacks(root, count: int) -> None:
    for _ in range(count):
        jev.log_decision(root, {"ts": jev._utc_stamp(), "outcome": "fallback", "fallback_reason": "timeout"})


TAIL = "operator_logs_hook_routes_tail"


@pytest.mark.parametrize("count, mode, entries, evidence", [
    (SELECTOR_FAILURE_LIMIT, "jev", (), "unavailable"),
    (SELECTOR_FAILURE_LIMIT - 1, "jev", (), TAIL),
    (SELECTOR_FAILURE_LIMIT, "jev", (line(),), TAIL),
    (SELECTOR_FAILURE_LIMIT, "shadow", (), TAIL),
], ids=["no_lines", "under_the_limit", "the_log_has_lines", "not_on_jev"])
def test_fallbacks_with_no_route_lines_say_the_log_is_not_evidence(tmp_path, count, mode, entries, evidence):
    root = tmp_path / "operator"
    root.mkdir()
    fallbacks(root, count)
    assert report_for(tmp_path, *entries, mode=mode)["metrics"]["hook_route_evidence"] == evidence


def test_a_log_path_that_cannot_take_a_line_does_not_read_as_no_failures(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    fallbacks(root, SELECTOR_FAILURE_LIMIT)
    route_log_path(root).mkdir()
    record_route(root, line())
    metrics = report_for(tmp_path)["metrics"]
    assert (metrics["selector_failures_in_a_row"], metrics["hook_route_evidence"]) == (0, "unavailable")


def test_a_working_selector_adds_no_reason(tmp_path):
    """The health hook's line at session start is built from these reasons alone."""
    (tmp_path / "plain").mkdir()
    (tmp_path / "live").mkdir()
    plain = report_for(tmp_path / "plain", mode="deterministic")
    live = report_for(tmp_path / "live", *ages(60, 70), {**empty(), "_age": 50})
    assert live["degraded_reasons"] == plain["degraded_reasons"]


def test_the_fallback_count_still_counts_what_it_counted(tmp_path):
    empty_pool = {"ts": jev._utc_stamp(), "outcome": "fallback", "fallback_reason": "no_candidates"}
    jev.log_decision(tmp_path, empty_pool)
    assert jev.decision_log_summary(tmp_path)["fallbacks_24h"] == 1


def test_the_new_counts_say_nothing_but_counts(tmp_path):
    report = report_for(tmp_path, *ages(60, 70), {**reply(), "_age": 80}, {**empty(), "_age": 90})
    assert report["metrics"]["hook_route_outcomes_24h"] == {
        "session_start": dict.fromkeys(routes.OUTCOMES, 0),
        "user_prompt_submit": {**dict.fromkeys(routes.OUTCOMES, 0), "delivered": 1, "failed": 2, "nothing_relevant": 1},
    }


@pytest.mark.parametrize("values, reasons", [
    ({"selector_mode": "jev"}, {KEY_REASON}),
    ({"selector_mode": "jev", "selector_key_present": True}, set()),
    ({"selector_mode": "jev", "selector_key_checked": False}, set()),
    ({"selector_mode": "shadow"}, set()),
    ({"selector_mode": "deterministic"}, set()),
    ({"selector_mode": "jev", "selector_key_present": True, "selector_failures_in_a_row": 2}, set()),
    ({"selector_mode": "jev", "selector_key_present": True, "selector_failures_in_a_row": 3}, {REPEATED_REASON}),
    ({"selector_mode": "jev", "selector_failures_in_a_row": 3}, {KEY_REASON, REPEATED_REASON}),
    ({"selector_mode": "shadow", "selector_failures_in_a_row": 9}, set()),
    ({"selector_failures_in_a_row": 9}, set()),
])
def test_the_rule_reads_only_jev(values, reasons):
    report = evaluate_health(HealthInputs(compiler_count=1, database_ok=True, migrations_ok=True, **values))
    assert set(report.degraded_reasons) == reasons
    assert report.status == ("red" if reasons else "green")


# -- end to end through the real hook ----------------------------------------------

class Source:
    records = (RetrievalRecord(
        record_id="urn:synthetic:judgment:1", text="A synthetic rule.", section="general",
        provenance_status="captured", authority_tier="captured_judgment", evidence_ids=("urn:synthetic:evidence:1",),
        case_ids=("urn:synthetic:case:1",), case_summaries=("Synthetic case",),
        source_receipt_ids=("urn:synthetic:receipt:1",), provenance_complete=True, ontology_type="Verdict",
        ontology_path=("judgment", "Verdict"), judgment_statement="A synthetic rule.",
        valid_from="2026-01-01T00:00:00Z",
    ),)

    def retrieval_candidates(self, snapshot_id, **kwargs):
        return self.records


class Jev:
    down = True

    def __call__(self, endpoint, body, headers, timeout):
        if self.down:
            raise TimeoutError("synthetic")
        questions = json.loads(body)["questions"]
        return json.dumps({"answers": {name: {"type": "noul", "noul": 0.9} for name in questions}}).encode("utf-8")


def live_hook(tmp_path, monkeypatch, capsys):
    """A real hook on one store, a fake Jev, and the store's health as the CLI reports it."""
    monkeypatch.setattr(retrieval, "StoreRetrievalSource", lambda store: Source())
    service = Jev()
    monkeypatch.setattr(jev, "http_post", service)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True, "selector": selector(),
    }))

    def turn(prompt):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"session_id": prompt, "prompt": prompt})))
        assert cli.main(["--config", str(config), "hook", "user-prompt-submit"]) == 0
        capsys.readouterr()

    def health():
        cli.main(["--config", str(config), "health"])
        return json.loads(capsys.readouterr().out)

    return service, turn, health


def test_a_dead_jev_degrades_on_the_third_failed_turn_and_clears_on_a_reply(tmp_path, monkeypatch, capsys):
    service, turn, health = live_hook(tmp_path, monkeypatch, capsys)
    turn("first")
    turn("second")
    assert REPEATED_REASON not in health()["degraded_reasons"]
    turn("third")
    assert REPEATED_REASON in health()["degraded_reasons"]
    service.down = False
    turn("fourth")
    assert REPEATED_REASON not in health()["degraded_reasons"]


def disk_full(self, *args, **kwargs):
    raise OSError(28, "synthetic: no space left on device")


def test_a_local_failure_after_each_reply_still_counts(tmp_path, monkeypatch, capsys):
    service, turn, health = live_hook(tmp_path, monkeypatch, capsys)
    service.down = False
    monkeypatch.setattr(DeliveryReceipts, "prepare_delivery", disk_full)
    for prompt in ("first", "second", "third"):
        turn(prompt)
    report = health()
    assert REPEATED_REASON in report["degraded_reasons"]
    assert report["metrics"]["selector_failures_in_a_row"] == 3
    assert report["metrics"]["hook_route_outcomes_24h"]["user_prompt_submit"]["failed"] == 3


def test_a_reply_a_local_step_then_failed_does_not_end_a_run(tmp_path, monkeypatch, capsys):
    service, turn, health = live_hook(tmp_path, monkeypatch, capsys)
    turn("first")
    turn("second")
    service.down = False
    with monkeypatch.context() as patch:
        patch.setattr(DeliveryReceipts, "prepare_delivery", disk_full)
        turn("third")
    service.down = True
    turn("fourth")
    assert health()["metrics"]["selector_failures_in_a_row"] == 4
