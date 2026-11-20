"""Every Stop leaves one content-free route line, and health can see capture go quiet."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import imprint.hook_routes as routes
from imprint import cli
from imprint.capture import marks, provenance, schema, t3_origin, transcript
from imprint.errors import ImprintError
from imprint.health import health_report
from imprint.hook_routes import (
    ROUTE_DEFER_ENV,
    STOP_OUTCOMES,
    STOP_SKIP_OUTCOMES,
    record_route,
    route_entry,
    route_log_path,
    route_summary,
)
from imprint.store import ImprintStore

ROOT = Path(__file__).resolve().parents[2]
FEEDBACK = "No, keep the source label in the summary."
# Words no line may hold.
MARKED = "No, keep the zebracopy-marker label in the summary."
LINE_KEYS = {"answered", "bytes", "detail", "method", "mode", "outcome", "route", "ts"}
ASSISTANT = {"type": "assistant", "message": {"content": "Draft."}}
DAY = 86400


@pytest.fixture(autouse=True)
def plain_stop(monkeypatch):
    for name in ("IMPRINT_CAPTURE_ORIGIN", ROUTE_DEFER_ENV, "IMPRINT_SELECTOR"):
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path: Path, **settings) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True, **settings,
    }))
    return path


def operator_root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "test"


def lines(tmp_path: Path) -> list[dict]:
    path = route_log_path(operator_root(tmp_path))
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def stop(tmp_path, monkeypatch, capsys, event: dict, config: Path | None = None) -> tuple[int, dict]:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    code = cli.main(["--config", str(config or write_config(tmp_path)), "hook", "stop-capture"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out else {}


def transcript_of(tmp_path: Path, *entries, name: str = "transcript.jsonl") -> str:
    path = tmp_path / name
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return str(path)


def typed(text: str = FEEDBACK, **extra) -> dict:
    return {"type": "user", "uuid": "user-1", "promptSource": "typed", "message": {"content": text}, **extra}


def one_line(tmp_path) -> dict:
    (line,) = lines(tmp_path)
    assert set(line) == LINE_KEYS
    assert line["route"] == "stop_capture"
    assert line["outcome"] in STOP_OUTCOMES
    assert (line["answered"], line["bytes"], line["method"]) == (False, 0, None)
    return line


# -- the vocabulary ------------------------------------------------------------------

def _reasons_the_stop_hook_can_write() -> set[str]:
    named = {
        provenance.SYNTHETIC_ENTRY_REASON, provenance.NO_OPERATOR_MESSAGE_REASON, provenance.MACHINE_ORIGIN_REASON,
        transcript.TRANSCRIPT_UNAVAILABLE_REASON, transcript.TRANSCRIPT_REFUSED_REASON,
        transcript.TRANSCRIPT_MALFORMED_REASON, transcript.TRANSCRIPT_TAIL_EXHAUSTED_REASON,
        marks.ALREADY_CAPTURED_REASON, marks.OPERATOR_TURN_EXPIRED_REASON, schema.OPERATOR_TEXT_OVERSIZED_REASON,
        "feedback_text_unavailable",
    }
    return named | {value for name, value in vars(t3_origin).items() if name.endswith("_REASON")}


def test_the_vocabulary_is_closed_and_has_no_repeats():
    assert len(STOP_OUTCOMES) == len(set(STOP_OUTCOMES))
    assert {"captured", "not_feedback", "origin_unverified", "failed"} <= set(STOP_OUTCOMES)
    assert set(STOP_SKIP_OUTCOMES) == _reasons_the_stop_hook_can_write()


@pytest.mark.parametrize("reason", sorted(_reasons_the_stop_hook_can_write()))
def test_each_skip_reason_is_an_outcome_of_its_own(reason):
    body = {"status": "skipped", "reason": reason, "provenance_basis": "unverified"}
    entry = route_entry("stop_capture", body, mode="deterministic")
    assert (entry["outcome"], entry["detail"]) == (reason, "unverified")


def test_every_reason_the_cli_writes_has_an_outcome():
    written = set(re.findall(r'"reason":\s*"([a-z_]+)"', (ROOT / "src" / "imprint" / "cli.py").read_text()))
    assert written
    for reason in written:
        outcome = route_entry("stop_capture", {"status": "skipped", "reason": reason}, mode=None)["outcome"]
        assert outcome != "failed" or reason == "hook_input_invalid", reason


@pytest.mark.parametrize("body, outcome, detail", [
    ({"status": "queued", "canonical_status": "compiled"}, "captured", "compiled"),
    ({"status": "queued", "canonical_status": "spool_only"}, "captured", "spool_only"),
    ({"status": "skipped", "reason": "not_explicit_feedback"}, "not_feedback", None),
    ({"status": "skipped", "reason": "unverified_provenance", "provenance_basis": "unverified"},
     "origin_unverified", "unverified"),
    ({"status": "skipped", "reason": "unverified_provenance", "provenance_basis": "transport"},
     "origin_unverified", "transport"),
    ({"status": "skipped", "reason": "hook_input_invalid"}, "failed", "hook_input_invalid"),
    ({"status": "skipped", "reason": "a_reason_added_later"}, "failed", "unknown_reason"),
    ({"status": "skipped", "reason": "failed"}, "failed", "unknown_reason"),
    ({"status": "skipped", "reason": "captured"}, "failed", "unknown_reason"),
    ({"status": "skipped", "reason": ["unverified_provenance"]}, "failed", "unknown_reason"),
    ({"status": "skipped"}, "failed", "unknown_reason"),
    ({"status": "error"}, "failed", "unknown_reason"),
    ({}, "failed", "unknown_reason"),
])
def test_every_stop_output_comes_to_one_outcome(body, outcome, detail):
    entry = route_entry("stop_capture", body, mode="jev")
    assert (entry["outcome"], entry["detail"], entry["mode"]) == (outcome, detail, "jev")
    assert set(entry) == LINE_KEYS


# -- one line for each way a Stop ends ------------------------------------------------

def _expired(tmp_path):
    return {"transcript_path": transcript_of(tmp_path, ASSISTANT, typed(timestamp="2020-01-01T00:00:00Z"))}


def _symlinked(tmp_path):
    target = transcript_of(tmp_path, ASSISTANT, typed(), name="real.jsonl")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    return {"transcript_path": str(link)}


def _malformed(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json\n" + json.dumps(typed()) + "\n")
    return {"transcript_path": str(path)}


PASTE_ONLY = '<pasted_content id="a1">\nNo, keep the vendor template.\n</pasted_content id="a1">'
OVERSIZED = "No, keep the source label. " + "z" * (256 * 1024)
ENDINGS = {
    "captured": (lambda tmp: {"transcript_path": transcript_of(tmp, ASSISTANT, typed())}, "compiled"),
    "not_feedback": (lambda tmp: {"transcript_path": transcript_of(tmp, ASSISTANT, typed("What time is it?"))}, None),
    "not_feedback_nothing_left": (lambda tmp: {"transcript_path": transcript_of(tmp, ASSISTANT, typed(PASTE_ONLY))}, None),
    "origin_unverified": (
        lambda tmp: {"transcript_path": transcript_of(tmp, ASSISTANT, typed(promptSource="sdk"))}, "unverified",
    ),
    "synthetic_transcript_entry": (
        lambda tmp: {"transcript_path": transcript_of(tmp, ASSISTANT, typed(promptSource="sdk", isSidechain=True))},
        "structure",
    ),
    "no_operator_message": (lambda tmp: {"transcript_path": transcript_of(tmp, ASSISTANT)}, None),
    "operator_turn_expired": (_expired, None),
    "feedback_text_unavailable": (lambda tmp: {}, None),
    "operator_text_oversized": (lambda tmp: {"operator_text": OVERSIZED}, None),
    "transcript_unavailable": (lambda tmp: {"transcript_path": str(tmp / "never-written.jsonl")}, None),
    "transcript_refused": (_symlinked, None),
    "transcript_malformed": (_malformed, None),
}


@pytest.mark.parametrize("ending", ENDINGS)
def test_each_way_a_stop_ends_writes_one_line(tmp_path, monkeypatch, capsys, ending):
    build, detail = ENDINGS[ending]
    code, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s", **build(tmp_path)})
    assert code == 0
    line = one_line(tmp_path)
    expected = "not_feedback" if ending.startswith("not_feedback") else ending
    assert (line["outcome"], line["detail"]) == (expected, detail)
    assert "_imprint_route" not in body


def test_a_capture_that_does_not_compile_says_so(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path, compiler=False)
    stop(tmp_path, monkeypatch, capsys, {"session_id": "s", "transcript_path": transcript_of(tmp_path, typed())}, config)
    assert (one_line(tmp_path)["outcome"], one_line(tmp_path)["detail"]) == ("captured", "spool_only")


def test_a_turn_already_captured_says_so(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    event = {"session_id": "s", "transcript_path": transcript_of(tmp_path, ASSISTANT, typed())}
    stop(tmp_path, monkeypatch, capsys, event, config)
    stop(tmp_path, monkeypatch, capsys, event, config)
    assert [line["outcome"] for line in lines(tmp_path)] == ["captured", "operator_turn_already_captured"]


def test_a_search_that_ran_out_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(transcript, "MAX_TRANSCRIPT_BYTES", 4096)
    monkeypatch.setattr(transcript, "TAIL_READ_BYTES", 1024)
    path = transcript_of(tmp_path, typed(), {"type": "assistant", "message": {"content": "x" * 9000}})
    code, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s", "transcript_path": path})
    assert (code, body["reason"]) == (0, "transcript_tail_exhausted")
    assert one_line(tmp_path)["outcome"] == "transcript_tail_exhausted"


def test_a_line_reports_the_selector_mode_in_force(tmp_path, monkeypatch, capsys):
    stop(tmp_path, monkeypatch, capsys, {"session_id": "s"}, write_config(tmp_path, selector={"mode": "shadow"}))
    assert one_line(tmp_path)["mode"] == "shadow"


# -- failures ------------------------------------------------------------------------

def _failing(exception):
    def raise_it(*args, **kwargs):
        raise exception
    return raise_it


def test_a_capture_that_raises_is_one_failed_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "write_envelope", _failing(ImprintError("synthetic")))
    code, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s", "transcript_path": transcript_of(tmp_path, typed())})
    assert (code, body["status"]) == (2, "error")
    assert (one_line(tmp_path)["outcome"], one_line(tmp_path)["detail"]) == ("failed", "hook_exception")


def test_an_event_of_the_wrong_kind_is_one_failed_line(tmp_path, monkeypatch, capsys):
    code, _ = stop(tmp_path, monkeypatch, capsys, {"session_id": "s", "hook_event_name": "SessionStart"})
    assert code == 2
    assert one_line(tmp_path)["outcome"] == "failed"


def test_an_event_too_deep_to_read_is_one_failed_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.json, "load", _failing(RecursionError()))
    code, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s"})
    assert (code, body["reason"]) == (0, "hook_input_invalid")
    assert (one_line(tmp_path)["outcome"], one_line(tmp_path)["detail"]) == ("failed", "hook_input_invalid")


# -- a Stop skipped before the config is read ----------------------------------------

@pytest.mark.parametrize("marker", ["automation", "other"])
def test_a_machine_skip_on_its_own_writes_nothing(tmp_path, monkeypatch, capsys, marker):
    monkeypatch.setenv("IMPRINT_CAPTURE_ORIGIN", marker)
    code, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s"})
    assert code == 0 and "_imprint_route" not in body
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("marker, outcome", [("automation", "machine_origin"), ("other", "origin_unverified")])
def test_a_machine_skip_hands_the_bridge_its_line(tmp_path, monkeypatch, capsys, marker, outcome):
    monkeypatch.setenv("IMPRINT_CAPTURE_ORIGIN", marker)
    monkeypatch.setenv(ROUTE_DEFER_ENV, "1")
    _, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s"})
    line = body["_imprint_route"]
    assert (line["route"], line["outcome"], line["detail"], line["mode"]) == ("stop_capture", outcome, "transport", None)
    assert not (tmp_path / "data").exists()


def test_a_machine_skip_too_deep_to_read_hands_the_bridge_a_failed_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IMPRINT_CAPTURE_ORIGIN", "automation")
    monkeypatch.setenv(ROUTE_DEFER_ENV, "1")
    monkeypatch.setattr(cli.json, "load", _failing(RecursionError()))
    _, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s"})
    assert (body["_imprint_route"]["outcome"], body["_imprint_route"]["detail"]) == ("failed", "hook_input_invalid")


def test_a_stop_under_the_bridge_hands_its_line_over(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ROUTE_DEFER_ENV, "1")
    _, body = stop(tmp_path, monkeypatch, capsys, {"session_id": "s", "transcript_path": transcript_of(tmp_path, typed())})
    assert body["_imprint_route"]["outcome"] == "captured"
    assert lines(tmp_path) == []


# -- under the bridge ----------------------------------------------------------------

def _bridge():
    spec = importlib.util.spec_from_file_location("imprint_test_stop_bridge", ROOT / "hooks" / "_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridged(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPRINT_CONFIG", str(write_config(tmp_path)))
    return _bridge()


def _child(bridge, monkeypatch, *, stdout="", code=0, raises=None):
    def run(argv, payload):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, code, stdout, "")
    monkeypatch.setattr(bridge, "_graceful_run", run)


def _run(bridge, monkeypatch, event="{}"):
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(event))
    return bridge.run("stop-capture")


def _failures(tmp_path):
    return sorted((operator_root(tmp_path) / "logs" / "hook-failures").glob("*.json"))


def test_the_bridge_writes_the_stop_line_and_keeps_it_from_the_host(tmp_path, bridged, monkeypatch, capsys):
    receipt = {"hook_schema_version": "1.0.0", "status": "skipped", "reason": "not_explicit_feedback"}
    line = route_entry("stop_capture", receipt, mode="deterministic")
    _child(bridged, monkeypatch, stdout=json.dumps({**receipt, "_imprint_route": line}))
    assert _run(bridged, monkeypatch) == 0
    assert capsys.readouterr().out == json.dumps(receipt, sort_keys=True) + "\n"
    assert lines(tmp_path) == [line]
    assert _failures(tmp_path) == []


def test_a_capture_that_failed_still_blocks_the_stop_once_and_leaves_a_failed_line(tmp_path, bridged, monkeypatch, capsys):
    _child(bridged, monkeypatch, stdout='{"status": "error"}', code=2)
    assert _run(bridged, monkeypatch) == 2
    assert json.loads(capsys.readouterr().out)["failure_policy"] == "fail_closed"
    assert [(line["outcome"], line["detail"]) for line in lines(tmp_path)] == [("failed", "hook_action_failed")]


@pytest.mark.parametrize("raises, error", [
    (subprocess.TimeoutExpired("imprint", 10), "hook_action_timeout"),
    (FileNotFoundError("synthetic"), "hook_executable_unavailable"),
], ids=["timeout", "unavailable"])
def test_a_stop_that_never_finished_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys, raises, error):
    _child(bridged, monkeypatch, raises=raises)
    assert _run(bridged, monkeypatch) == 0
    assert [(line["route"], line["outcome"], line["detail"]) for line in lines(tmp_path)] == [
        ("stop_capture", "failed", error),
    ]


def test_a_stop_child_that_reports_no_line_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys):
    _child(bridged, monkeypatch, stdout='{"status": "skipped", "reason": "not_explicit_feedback"}')
    assert _run(bridged, monkeypatch) == 0
    assert [(line["outcome"], line["detail"]) for line in lines(tmp_path)] == [("failed", "hook_route_unreported")]
    assert len(_failures(tmp_path)) == 1


def test_a_stop_event_the_bridge_cannot_read_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys):
    assert _run(bridged, monkeypatch, "{not json") == 0
    assert [(line["outcome"], line["detail"]) for line in lines(tmp_path)] == [("failed", "hook_input_invalid")]


def test_a_real_stop_through_the_bridge_gives_the_host_what_it_always_did(tmp_path):
    environment = {**os.environ, "IMPRINT_CONFIG": str(write_config(tmp_path))}
    environment.pop("IMPRINT_CAPTURE_ORIGIN", None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "stop_capture.py")], input='{"session_id": "s"}',
        capture_output=True, text=True, env=environment, check=True,
    )
    assert result.stdout == json.dumps(
        {"hook_schema_version": "1.0.0", "reason": "feedback_text_unavailable", "status": "skipped"}, sort_keys=True,
    ) + "\n"
    assert one_line(tmp_path)["outcome"] == "feedback_text_unavailable"


def test_a_real_machine_stop_through_the_bridge_is_counted(tmp_path):
    environment = {**os.environ, "IMPRINT_CONFIG": str(write_config(tmp_path)), "IMPRINT_CAPTURE_ORIGIN": "automation"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "stop_capture.py")], input='{"session_id": "s"}',
        capture_output=True, text=True, env=environment, check=True,
    )
    assert not any(key.startswith("_imprint") for key in json.loads(result.stdout))
    line = one_line(tmp_path)
    assert (line["outcome"], line["mode"]) == ("machine_origin", None)
    assert _failures(tmp_path) == []


# -- content ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [typed(MARKED), typed(MARKED, promptSource="sdk"), typed(f"{MARKED} Is that right?")])
def test_a_line_holds_no_words(tmp_path, monkeypatch, capsys, entry):
    stop(tmp_path, monkeypatch, capsys, {"session_id": "zebracopy-session", "transcript_path": transcript_of(tmp_path, entry)})
    text = route_log_path(operator_root(tmp_path)).read_text()
    assert "zebracopy" not in text and "transcript" not in text
    assert set(one_line(tmp_path)) == LINE_KEYS


def test_stop_lines_share_the_file_and_its_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "ROUTE_LOG_MAX_BYTES", 400)
    for _ in range(3):  # two lines fill the cap, so the third moves them aside
        record_route(tmp_path, route_entry("stop_capture", {"status": "skipped", "reason": "not_explicit_feedback"}, mode=None))
    path = route_log_path(tmp_path)
    assert [len(path.with_name(name).read_text().splitlines()) for name in ("hook-routes.jsonl.1", path.name)] == [2, 1]
    assert route_summary(tmp_path)["outcomes_24h"]["stop_capture"]["not_feedback"] == 3


# -- health ----------------------------------------------------------------------------

def _stamp(age: float, now: float) -> str:
    return datetime.fromtimestamp(now - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stop_line(outcome="origin_unverified", age=60.0, route="stop_capture", now=None) -> dict:
    current = time.time() if now is None else now
    return {
        "answered": False, "bytes": 0, "detail": None, "method": None, "mode": "deterministic",
        "outcome": outcome, "route": route, "ts": _stamp(age, current),
    }


def _report(tmp_path, *entries, raw: str = "", spool_ages=(), **config) -> dict:
    root = tmp_path / "operator"
    root.mkdir(exist_ok=True)
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    for entry in entries:
        record_route(root, entry)
    if raw:
        with route_log_path(root).open("a") as handle:
            handle.write(raw)
    now = time.time()
    for index, age in enumerate(spool_ages):
        path = root / "spool" / "primary" / f"capture-{index}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        os.utime(path, (now - age, now - age))
    return health_report(root, store, {"compiler": True, "hooks_dir": str(tmp_path), **config})


def test_health_counts_each_stop_outcome_for_the_last_day(tmp_path):
    report = _report(tmp_path, _stop_line("captured"), _stop_line("not_feedback"), _stop_line("not_feedback"),
                     _stop_line("t3_entrypoint_unknown"), _stop_line("failed"), _stop_line("captured", age=2 * DAY))
    counts = report["metrics"]["hook_route_outcomes_24h"]["stop_capture"]
    assert set(counts) == set(STOP_OUTCOMES)
    assert {key: value for key, value in counts.items() if value} == {
        "captured": 1, "not_feedback": 2, "t3_entrypoint_unknown": 1, "failed": 1,
    }


def test_health_ages_the_newest_capture(tmp_path):
    (tmp_path / "none").mkdir()
    (tmp_path / "some").mkdir()
    assert _report(tmp_path / "none")["metrics"]["capture_newest_age_seconds"] == -1
    age = _report(tmp_path / "some", spool_ages=(5 * DAY, 100))["metrics"]["capture_newest_age_seconds"]
    assert 100 <= age < 200


def test_a_real_capture_is_the_newest_capture(tmp_path, monkeypatch, capsys):
    stop(tmp_path, monkeypatch, capsys, {"session_id": "s", "transcript_path": transcript_of(tmp_path, typed())})
    root = operator_root(tmp_path)
    report = health_report(root, ImprintStore(root / "imprint.db"), {"compiler": True})
    assert 0 <= report["metrics"]["capture_newest_age_seconds"] < 60
    assert report["metrics"]["hook_route_outcomes_24h"]["stop_capture"]["captured"] == 1
