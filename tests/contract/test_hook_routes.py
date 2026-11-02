"""Every turn of a delivering hook lands on exactly one outcome, and says which.

Records are synthetic and every request goes to a fake transport.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import imprint.cli as cli
import imprint.hook_routes as routes
import imprint.retrieve as retrieval
from imprint.capture.t3_origin import T3_SCRATCH_THREAD_REASON
from imprint.config import DEFAULTS, SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START
from imprint.errors import ImprintError
from imprint.hook_routes import OUTCOMES, route_entry, route_log_path
from imprint.retrieve import jev
from imprint.retrieve.models import RetrievalRecord
from imprint.retrieve.receipts import DeliveryReceipts

ROOT = Path(__file__).parents[2]
KEY_ENV = "JEV_TEST_KEY"
PROMPT = "Should the synthetic prompt words ever reach a log?"
LINE_KEYS = {"answered", "bytes", "detail", "method", "mode", "outcome", "route", "ts"}
START = "session-start"
SUBMIT = "user-prompt-submit"
IN_DOMAIN = {"session_id": "s", "prompt": PROMPT, "domain_id": "research"}
NO_DOMAIN = {"session_id": "s", "prompt": PROMPT}


def record(index: int, text: str, section: str = "general", **overrides) -> RetrievalRecord:
    values = dict(
        record_id=f"urn:synthetic:judgment:{index:04d}", text=text, section=section,
        provenance_status="captured", authority_tier="captured_judgment",
        evidence_ids=("urn:synthetic:evidence:1",), case_ids=("urn:synthetic:case:1",),
        case_summaries=("Synthetic case summary",), source_receipt_ids=("urn:synthetic:receipt:1",),
        provenance_complete=True, ontology_type="Verdict", ontology_path=("judgment", "Verdict"),
        judgment_statement=text, valid_from="2026-01-01T00:00:00Z",
    )
    values.update(overrides)
    return RetrievalRecord(**values)


RECORDS = [
    record(1, "A synthetic rule for every thread."),
    record(2, "A synthetic rule for the research domain.", "domain", domain_id="research"),
]


class Source:
    def __init__(self, records):
        self.records = records

    def retrieval_candidates(self, snapshot_id, **kwargs):
        return tuple(self.records)


class Transport:
    def __init__(self):
        self.calls = 0
        self.score = 0.9
        self.error: Exception | None = None
        self.raw: bytes | None = None

    def __call__(self, endpoint, body, headers, timeout):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        questions = json.loads(body)["questions"]
        return json.dumps({"answers": {name: {"type": "noul", "noul": self.score} for name in questions}}).encode("utf-8")


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(jev, "http_post", fake)
    return fake


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")
    use_records(monkeypatch, RECORDS)


def use_records(monkeypatch, records):
    monkeypatch.setattr(retrieval, "StoreRetrievalSource", lambda store: Source(records))


def selector(mode: str = "jev", **jev_values) -> dict:
    value = copy.deepcopy(DEFAULTS["selector"])
    value["mode"] = mode
    value["jev"]["api_key_env"] = KEY_ENV
    value["jev"].update(jev_values)
    return value


def write_config(tmp_path: Path, mode: str = "deterministic", settings: dict | None = None, **jev_values) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True,
        "domains": [{"domain_id": "research", "public_label": "Research"}],
        "selector": selector(mode, **jev_values), **(settings or {}),
    }))
    return path


def operator_root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "test"


def hook(config: Path, action: str, event: dict, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    assert cli.main(["--config", str(config), "hook", action]) == 0
    return json.loads(capsys.readouterr().out)


def route_lines(tmp_path: Path) -> list[dict]:
    path = route_log_path(operator_root(tmp_path))
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def one_turn(tmp_path, monkeypatch, capsys, action, event, *, mode="jev", **jev_values) -> dict:
    """Run one turn on a fresh store, and return its only line."""
    hook(write_config(tmp_path, mode, **jev_values), action, event, monkeypatch, capsys)
    (line,) = route_lines(tmp_path)
    assert set(line) == LINE_KEYS
    assert line["outcome"] in OUTCOMES
    return line


# -- session start -----------------------------------------------------------------

@pytest.mark.parametrize("mode", ["deterministic", "jev", "shadow"])
def test_session_start_delivers_and_then_has_already_delivered(tmp_path, monkeypatch, capsys, mode):
    config = write_config(tmp_path, mode)
    first = hook(config, START, {"session_id": "s"}, monkeypatch, capsys)
    hook(config, START, {"session_id": "s"}, monkeypatch, capsys)
    (delivered, repeat) = route_lines(tmp_path)
    assert (delivered["outcome"], delivered["route"], delivered["method"], delivered["mode"]) == (
        "delivered", "session_start", "deterministic", mode,
    )
    assert delivered["bytes"] == len(first["hookSpecificOutput"]["additionalContext"].encode("utf-8")) > 0
    assert (repeat["outcome"], repeat["bytes"]) == ("already_delivered", 0)


def test_session_start_on_an_empty_store_is_nothing_relevant(tmp_path, monkeypatch, capsys):
    use_records(monkeypatch, [])
    line = one_turn(tmp_path, monkeypatch, capsys, START, {"session_id": "s"})
    assert (line["outcome"], line["bytes"]) == ("nothing_relevant", 0)


def test_a_session_start_refresh_records_what_it_delivered(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    hook(config, START, {"session_id": "s"}, monkeypatch, capsys)
    hook(config, START, {"session_id": "s", "source": "compact"}, monkeypatch, capsys)
    assert [line["outcome"] for line in route_lines(tmp_path)] == ["delivered", "delivered"]


# -- deterministic prompts ---------------------------------------------------------

def test_a_deterministic_prompt_with_no_domain_is_nothing_relevant(tmp_path, monkeypatch, capsys):
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN, mode="deterministic")
    assert (line["outcome"], line["detail"], line["method"], line["answered"]) == (
        "nothing_relevant", "domain_no_match", "deterministic", False,
    )


def test_a_deterministic_prompt_delivers_its_domain_and_then_already_has(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    (delivered, repeat) = route_lines(tmp_path)
    assert delivered["outcome"] == "delivered" and delivered["bytes"] > 0
    assert repeat["outcome"] == "already_delivered"


def test_an_empty_prompt_under_jev_asks_nothing_and_says_so(tmp_path, transport, monkeypatch, capsys):
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, {"session_id": "s", "prompt": "   "})
    assert (line["outcome"], line["method"], line["mode"]) == ("nothing_relevant", "deterministic", "jev")
    assert transport.calls == 0


# -- Jev's own answers -------------------------------------------------------------

def test_picks_are_delivered_by_a_reply(tmp_path, transport, monkeypatch, capsys):
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (line["outcome"], line["detail"], line["method"], line["answered"]) == ("delivered", "picked", "jev", True)
    assert line["bytes"] > 0


def test_no_picks_is_nothing_relevant_from_a_reply(tmp_path, transport, monkeypatch, capsys):
    transport.score = 0.05
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (line["outcome"], line["detail"], line["answered"], line["bytes"]) == ("nothing_relevant", "no_picks", True, 0)


def test_no_picks_beside_local_records_still_delivered(tmp_path, transport, monkeypatch, capsys):
    transport.score = 0.05
    local = record(3, "A local rule Jev is never sent.", "domain", domain_id="research", ontology_type="Rule",
                   ontology_partition="business_declared", judgment_statement=None)
    use_records(monkeypatch, [*RECORDS, local])
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, IN_DOMAIN)
    assert (line["outcome"], line["detail"], line["answered"]) == ("delivered", "no_picks", True)


def test_a_replayed_turn_is_already_delivered_and_did_not_ask(tmp_path, transport, monkeypatch, capsys):
    config = write_config(tmp_path, "jev")
    hook(config, SUBMIT, NO_DOMAIN, monkeypatch, capsys)
    hook(config, SUBMIT, NO_DOMAIN, monkeypatch, capsys)
    (first, replay) = route_lines(tmp_path)
    assert (first["answered"], replay["outcome"], replay["answered"]) == (True, "already_delivered", False)
    assert transport.calls == 1


def test_everything_already_in_the_session_did_not_ask_jev(tmp_path, transport, monkeypatch, capsys):
    config = write_config(tmp_path, "jev")
    hook(config, START, {"session_id": "s"}, monkeypatch, capsys)
    hook(config, SUBMIT, NO_DOMAIN, monkeypatch, capsys)
    line = route_lines(tmp_path)[-1]
    assert (line["outcome"], line["detail"], line["answered"]) == ("nothing_relevant", "no_picks", False)
    assert transport.calls == 0


# -- empty is not dead -------------------------------------------------------------

def test_an_empty_pool_sends_nothing_and_is_nothing_relevant(tmp_path, transport, monkeypatch, capsys):
    use_records(monkeypatch, [])
    body_line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (body_line["outcome"], body_line["detail"], body_line["answered"]) == (
        "nothing_relevant", "no_candidates", False,
    )
    assert body_line["method"] == "deterministic_fallback"
    assert transport.calls == 0


def test_an_empty_pool_with_a_matched_domain_is_nothing_relevant_too(tmp_path, transport, monkeypatch, capsys):
    use_records(monkeypatch, [])
    assert one_turn(tmp_path, monkeypatch, capsys, SUBMIT, IN_DOMAIN)["outcome"] == "nothing_relevant"


def test_an_empty_pool_with_no_key_is_still_a_failure(tmp_path, transport, monkeypatch, capsys):
    use_records(monkeypatch, [])
    monkeypatch.delenv(KEY_ENV)
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (line["outcome"], line["detail"]) == ("failed", "missing_key")


# -- a turn that repeats one -------------------------------------------------------

LINES = pytest.mark.parametrize(
    "settings", [{}, {SHORT_LINES_PROMPT: True, SHORT_LINES_SESSION_START: True}], ids=["whole_lines", "short_lines"],
)
WIDE = record(4, "A rule with a long trail.", evidence_ids=tuple(f"urn:synthetic:evidence:{n}" for n in range(2000)))


def facts(tmp_path, *fields) -> list[tuple]:
    return [tuple(line[field] for field in fields) for line in route_lines(tmp_path)]


def prompted(number: int, event: dict = IN_DOMAIN) -> dict:
    return {**event, "prompt": f"synthetic prompt number {number}"}


@LINES
@pytest.mark.parametrize("mode, detail", [("jev", "no_candidates"), ("deterministic", None)])
def test_a_domain_with_no_records_stays_nothing_relevant_turn_after_turn(
    tmp_path, transport, monkeypatch, capsys, mode, detail, settings,
):
    use_records(monkeypatch, [])
    config = write_config(tmp_path, mode, settings)
    bodies = [hook(config, SUBMIT, prompted(number), monkeypatch, capsys) for number in range(3)]
    assert [item["status"] for item in bodies] == ["delivered", "already_delivered", "already_delivered"]
    assert facts(tmp_path, "outcome", "detail", "bytes") == [("nothing_relevant", detail, 0)] * 3
    assert transport.calls == 0


@LINES
def test_a_domain_that_only_has_records_elsewhere_stays_nothing_relevant(tmp_path, monkeypatch, capsys, settings):
    use_records(monkeypatch, RECORDS[:1])
    config = write_config(tmp_path, "deterministic", settings)
    for number in range(2):
        hook(config, SUBMIT, prompted(number), monkeypatch, capsys)
    assert facts(tmp_path, "outcome") == [("nothing_relevant",)] * 2


@LINES
@pytest.mark.parametrize("mode", ["deterministic", "jev", "shadow"])
def test_a_second_session_start_on_an_empty_store_is_still_nothing_relevant(tmp_path, monkeypatch, capsys, mode, settings):
    use_records(monkeypatch, [])
    config = write_config(tmp_path, mode, settings)
    bodies = [hook(config, START, {"session_id": "s"}, monkeypatch, capsys) for _ in range(2)]
    assert bodies[1]["status"] == "already_delivered"
    assert facts(tmp_path, "outcome") == [("nothing_relevant",)] * 2


@LINES
def test_no_picks_asked_twice_is_no_picks_twice(tmp_path, transport, monkeypatch, capsys, settings):
    transport.score = 0.05
    config = write_config(tmp_path, "jev", settings)
    bodies = [hook(config, SUBMIT, NO_DOMAIN, monkeypatch, capsys) for _ in range(2)]
    assert bodies[1]["selector_outcome"] == "already_delivered"
    assert facts(tmp_path, "outcome", "detail", "answered") == [
        ("nothing_relevant", "no_picks", True), ("nothing_relevant", "no_picks", False),
    ]
    assert transport.calls == 1


def test_picks_that_cannot_fit_stay_failed_on_a_repeat(tmp_path, transport, monkeypatch, capsys):
    use_records(monkeypatch, [WIDE])
    config = write_config(tmp_path, "jev")
    for _ in range(2):
        hook(config, SUBMIT, NO_DOMAIN, monkeypatch, capsys)
    assert facts(tmp_path, "outcome", "detail", "bytes", "method", "answered") == [
        ("failed", "oversized_picks", 0, "jev", True), ("failed", "oversized_picks", 0, "jev", False),
    ]
    assert transport.calls == 1


def test_a_repeat_of_real_picks_is_already_delivered(tmp_path, transport, monkeypatch, capsys):
    config = write_config(tmp_path, "jev")
    for _ in range(3):
        hook(config, SUBMIT, NO_DOMAIN, monkeypatch, capsys)
    assert facts(tmp_path, "outcome") == [("delivered",), ("already_delivered",), ("already_delivered",)]


def test_a_receipt_that_cannot_be_read_leaves_the_turn_already_delivered(tmp_path, monkeypatch, capsys):
    use_records(monkeypatch, [])
    config = write_config(tmp_path)
    hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    (receipt,) = (operator_root(tmp_path) / "receipts").rglob("*-domain-research.json")
    receipt.write_text("not a receipt")
    assert hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)["status"] == "already_delivered"
    assert facts(tmp_path, "outcome")[-1] == ("already_delivered",)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_a_pipe_at_a_receipt_path_cannot_hold_a_turn_open(tmp_path):
    receipts = DeliveryReceipts(tmp_path)
    _, final = receipts._paths("synthetic-session", "snapshot", None)
    os.mkfifo(final)
    found = []
    reader = threading.Thread(
        target=lambda: found.append(receipts.committed_response("synthetic-session", "snapshot", None)), daemon=True,
    )
    reader.start()
    reader.join(5)
    assert found == [None]


def test_reading_a_receipt_never_raises(tmp_path):
    assert DeliveryReceipts(tmp_path).committed_response("not a safe session", "snapshot", None) is None
    assert retrieval.committed_delivery(root=tmp_path / "absent", session_id="s", snapshot_id="snapshot") is None


# -- failed selection --------------------------------------------------------------

FAILURES = {
    "timeout": lambda transport, monkeypatch: setattr(transport, "error", TimeoutError("synthetic")),
    "http_error": lambda transport, monkeypatch: setattr(transport, "error", ConnectionError("synthetic")),
    "bad_response": lambda transport, monkeypatch: setattr(transport, "raw", b"not json"),
    "missing_key": lambda transport, monkeypatch: monkeypatch.delenv(KEY_ENV),
}


@pytest.mark.parametrize("reason", FAILURES)
def test_a_failed_selection_with_no_domain_is_failed_and_the_agent_gets_nothing(
    tmp_path, transport, monkeypatch, capsys, reason,
):
    FAILURES[reason](transport, monkeypatch)
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (line["outcome"], line["detail"], line["method"]) == ("failed", reason, "deterministic_fallback")
    assert (line["bytes"], line["answered"]) == (0, False)


@pytest.mark.parametrize("reason", FAILURES)
def test_a_failed_selection_stays_failed_when_the_fallback_delivers(tmp_path, transport, monkeypatch, capsys, reason):
    FAILURES[reason](transport, monkeypatch)
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, IN_DOMAIN)
    assert (line["outcome"], line["detail"]) == ("failed", reason)
    assert line["bytes"] > 0


def test_a_failed_selection_stays_failed_when_the_fallback_already_delivered(tmp_path, transport, monkeypatch, capsys):
    transport.error = TimeoutError("synthetic")
    config = write_config(tmp_path, "jev")
    hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    body = hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    assert body["status"] == "already_delivered"
    assert [line["outcome"] for line in route_lines(tmp_path)] == ["failed", "failed"]


def test_an_unsafe_key_file_is_a_failed_selection(tmp_path, transport, monkeypatch, capsys):
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic")
    key.chmod(0o644)
    monkeypatch.delenv(KEY_ENV)
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN, api_key_file=str(key))
    assert (line["outcome"], line["detail"]) == ("failed", "key_file_unsafe")


def test_a_failure_after_jev_answered_is_failed(tmp_path, transport, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(retrieval, "_jev_response", boom)
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (line["outcome"], line["detail"], line["answered"]) == ("failed", "exception", False)


def test_a_selector_that_cannot_load_is_failed(tmp_path, transport, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "imprint.retrieve.jev", None)
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (line["outcome"], line["detail"], line["method"]) == ("failed", "exception", "deterministic_fallback")


def test_picks_that_cannot_fit_are_failed_not_empty(tmp_path, transport, monkeypatch, capsys):
    use_records(monkeypatch, [WIDE])
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN)
    assert (line["outcome"], line["detail"], line["method"]) == ("failed", "oversized_picks", "jev")
    assert (line["bytes"], line["answered"]) == (0, True)


# -- shadow and scratch ------------------------------------------------------------

def test_a_shadow_turn_reports_the_deterministic_delivery(tmp_path, transport, monkeypatch, capsys):
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, IN_DOMAIN, mode="shadow")
    assert (line["outcome"], line["method"], line["mode"], line["detail"]) == ("delivered", "shadow", "shadow", "picked")


def test_a_shadow_turn_with_no_domain_delivers_nothing(tmp_path, transport, monkeypatch, capsys):
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN, mode="shadow")
    assert (line["outcome"], line["bytes"]) == ("nothing_relevant", 0)


def test_a_shadow_failure_is_failed_with_what_the_agent_got(tmp_path, transport, monkeypatch, capsys):
    transport.error = TimeoutError("synthetic")
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, IN_DOMAIN, mode="shadow")
    assert (line["outcome"], line["detail"], line["method"]) == ("failed", "timeout", "shadow")
    assert line["bytes"] > 0


@pytest.mark.parametrize("event, delivers", [(NO_DOMAIN, False), (IN_DOMAIN, True)], ids=["no_domain", "domain"])
@pytest.mark.parametrize("mode", ["jev", "shadow"])
def test_a_scratch_turn_is_a_scratch_skip(tmp_path, transport, monkeypatch, capsys, mode, event, delivers):
    monkeypatch.setattr(cli, "_t3_scratch_thread", lambda config, session: True)
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, event, mode=mode)
    assert (line["outcome"], line["method"], line["mode"]) == ("scratch_skip", "deterministic", mode)
    assert (line["bytes"] > 0) is delivers
    assert transport.calls == 0


def test_a_scratch_turn_stays_a_scratch_skip_when_its_domain_is_already_delivered(tmp_path, transport, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_t3_scratch_thread", lambda config, session: True)
    config = write_config(tmp_path, "jev")
    hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    assert [line["outcome"] for line in route_lines(tmp_path)] == ["scratch_skip", "scratch_skip"]


def test_a_store_with_no_selector_never_asks_about_scratch(tmp_path, transport, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_t3_scratch_thread", lambda config, session: True)
    assert one_turn(tmp_path, monkeypatch, capsys, SUBMIT, IN_DOMAIN, mode="deterministic")["outcome"] == "delivered"


# -- the closed set ----------------------------------------------------------------

def body(status="delivered", payload="", **fields) -> dict:
    return {"status": status, "hookSpecificOutput": {"additionalContext": payload}, **fields}


@pytest.mark.parametrize("output, outcome, detail", [
    (body(payload="x"), "delivered", None),
    (body(payload="x", selector_method="jev", selector_outcome="picked"), "delivered", "picked"),
    (body(payload="x", selector_outcome="crowded_picks"), "delivered", "crowded_picks"),
    (body(payload="x", selector_outcome="oversized_picks"), "delivered", "oversized_picks"),
    (body(payload="x", selector_outcome="no_picks"), "delivered", "no_picks"),
    (body(payload="x", selector_outcome="fallback", fallback_reason="no_candidates"), "delivered", "no_candidates"),
    (body(), "nothing_relevant", None),
    (body(selector_outcome="no_picks"), "nothing_relevant", "no_picks"),
    (body("skipped", reason="domain_no_match"), "nothing_relevant", "domain_no_match"),
    (body("skipped", reason="domain_no_match", fallback_reason="no_candidates", selector_outcome="fallback"),
     "nothing_relevant", "no_candidates"),
    (body("already_delivered"), "already_delivered", None),
    (body("already_delivered", selector_outcome="fallback", fallback_reason="no_candidates"), "already_delivered", None),
    (body("skipped", selector_outcome=T3_SCRATCH_THREAD_REASON), "scratch_skip", None),
    (body(payload="x", selector_outcome=T3_SCRATCH_THREAD_REASON), "scratch_skip", None),
    (body("already_delivered", selector_outcome=T3_SCRATCH_THREAD_REASON), "scratch_skip", None),
    (body("skipped", fallback_reason="timeout", selector_outcome="fallback"), "failed", "timeout"),
    (body(payload="x", fallback_reason="timeout", selector_outcome="fallback"), "failed", "timeout"),
    (body("already_delivered", fallback_reason="timeout", selector_outcome="fallback"), "failed", "timeout"),
    (body(selector_outcome="oversized_picks"), "failed", "oversized_picks"),
    (body("skipped", reason="hook_input_invalid"), "failed", "hook_input_invalid"),
    # A reason nobody has added yet must not pass as an empty turn.
    (body("skipped", fallback_reason="a_future_reason"), "failed", "a_future_reason"),
])
def test_every_hook_output_comes_to_one_outcome(output, outcome, detail):
    entry = route_entry("user_prompt_submit", output, mode="jev")
    assert (entry["outcome"], entry["detail"]) == (outcome, detail)
    assert entry["outcome"] in OUTCOMES


@pytest.mark.parametrize("delivery, outcome, detail", [
    (None, "already_delivered", None),
    ({"selected_ids": ["urn:synthetic:judgment:1"]}, "already_delivered", None),
    ({"selected_ids": ["urn:synthetic:judgment:1"], "selector_outcome": "oversized_picks"}, "already_delivered", None),
    ({"selected_ids": []}, "nothing_relevant", None),
    ({"selected_ids": [], "selector_outcome": "no_picks"}, "nothing_relevant", "no_picks"),
    ({"selected_ids": [], "selector_outcome": "oversized_picks"}, "failed", "oversized_picks"),
    ({"selected_ids": [], "selector_outcome": "crowded_picks"}, "failed", "crowded_picks"),
])
def test_a_latch_is_only_as_good_as_what_its_receipt_delivered(delivery, outcome, detail):
    entry = route_entry("user_prompt_submit", body("already_delivered"), mode="jev", delivery=delivery)
    assert (entry["outcome"], entry["detail"]) == (outcome, detail)


def test_each_fallback_reason_is_failed_but_the_empty_pool():
    for reason in jev.FALLBACK_REASONS:
        entry = route_entry("user_prompt_submit", body(fallback_reason=reason), mode="jev")
        assert entry["outcome"] == ("nothing_relevant" if reason == "no_candidates" else "failed")


def test_the_scratch_outcome_is_the_one_t3_reports():
    assert routes.SCRATCH_OUTCOME == T3_SCRATCH_THREAD_REASON


# -- exactly one line, and what is in it -------------------------------------------

def test_a_line_holds_no_words_or_ids(tmp_path, transport, monkeypatch, capsys):
    config = write_config(tmp_path, "jev")
    hook(config, START, {"session_id": "session-words"}, monkeypatch, capsys)
    hook(config, SUBMIT, {**IN_DOMAIN, "session_id": "session-words"}, monkeypatch, capsys)
    transport.error = TimeoutError("synthetic words")
    hook(config, SUBMIT, {**IN_DOMAIN, "prompt": "another synthetic prompt", "session_id": "session-words"}, monkeypatch, capsys)
    raw = route_log_path(operator_root(tmp_path)).read_text()
    assert raw.count("\n") == 3
    for secret in (PROMPT, "synthetic prompt", "synthetic rule", "urn:", "research", "session-words", str(tmp_path)):
        assert secret not in raw


def test_the_log_is_private(tmp_path, monkeypatch, capsys):
    hook(write_config(tmp_path), START, {"session_id": "s"}, monkeypatch, capsys)
    path = route_log_path(operator_root(tmp_path))
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0


def test_turning_the_decision_log_off_leaves_the_route_log(tmp_path, transport, monkeypatch, capsys):
    line = one_turn(tmp_path, monkeypatch, capsys, SUBMIT, NO_DOMAIN, log_decisions=False)
    assert line["outcome"] == "delivered"
    assert not jev.decision_log_path(operator_root(tmp_path)).exists()


def test_the_log_rotates_at_its_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "ROUTE_LOG_MAX_BYTES", 400)
    entry = routes.failed_entry(START, "hook_exception", mode="jev")
    for _ in range(40):
        routes.record_route(tmp_path, entry)
    current = route_log_path(tmp_path)
    assert current.stat().st_size <= 400
    assert current.with_name(current.name + ".1").stat().st_size <= 400


def test_a_log_that_cannot_be_written_changes_nothing_delivered(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    expected = hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    other = tmp_path / "other"
    other.mkdir()
    config = write_config(other)
    root = operator_root(other)
    root.mkdir(parents=True)
    (root / "logs").write_text("in the way")
    assert hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys) == expected


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_a_pipe_at_the_log_path_cannot_hold_a_hook_open(tmp_path):
    path = route_log_path(tmp_path)
    path.parent.mkdir()
    os.mkfifo(path)
    writer = threading.Thread(
        target=routes.record_route, args=(tmp_path, routes.failed_entry(START, "hook_exception", mode="jev")), daemon=True,
    )
    writer.start()
    writer.join(5)
    assert not writer.is_alive()


def test_a_line_that_cannot_be_built_changes_nothing_delivered(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    expected = hook(config, SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    other = tmp_path / "other"
    other.mkdir()

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(routes, "route_entry", boom)
    assert hook(write_config(other), SUBMIT, IN_DOMAIN, monkeypatch, capsys) == expected
    (line,) = route_lines(other)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_exception")


# -- a hook that raises ------------------------------------------------------------

def failing(exception):
    def fail(*args, **kwargs):
        raise exception

    return fail


def run_expecting(config, action, event, monkeypatch) -> int:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    return cli.main(["--config", str(config), "hook", action])


@pytest.mark.parametrize("action, event", [(START, {"session_id": "s"}), (SUBMIT, IN_DOMAIN)], ids=["start", "prompt"])
def test_a_hook_error_is_one_failed_line(tmp_path, monkeypatch, capsys, action, event):
    config = write_config(tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_payload", failing(ImprintError("synthetic")))
    assert run_expecting(config, action, event, monkeypatch) == 2
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"], line["bytes"]) == ("failed", "hook_exception", 0)


@pytest.mark.parametrize("exception, kind", [(RuntimeError("synthetic"), RuntimeError), (SystemExit(143), SystemExit)])
def test_a_raised_exit_or_crash_is_one_failed_line(tmp_path, monkeypatch, capsys, exception, kind):
    config = write_config(tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_payload", failing(exception))
    with pytest.raises(kind):
        run_expecting(config, START, {"session_id": "s"}, monkeypatch)
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_exception")


def test_a_turn_that_raises_after_settling_is_not_delivered(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    monkeypatch.setattr(cli, "_emit_retrieval_json", failing(OSError("synthetic pipe")))
    assert run_expecting(config, START, {"session_id": "s"}, monkeypatch) == 2
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_exception")


def test_an_event_of_the_wrong_kind_is_one_failed_line(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    assert run_expecting(config, SUBMIT, {"session_id": "s", "hook_schema_version": "9.9.9"}, monkeypatch) == 2
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["route"]) == ("failed", "user_prompt_submit")


def test_an_event_too_deep_to_read_is_one_failed_line(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    monkeypatch.setattr(cli.json, "load", failing(RecursionError()))
    assert run_expecting(config, SUBMIT, NO_DOMAIN, monkeypatch) == 0
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_input_invalid")


@pytest.mark.parametrize("action", ["stop-capture", "health-check"])
def test_a_hook_that_delivers_no_memory_has_no_line(tmp_path, monkeypatch, capsys, action):
    config = write_config(tmp_path)
    run_expecting(config, action, {"session_id": "s"}, monkeypatch)
    assert route_lines(tmp_path) == []


# -- under the bridge --------------------------------------------------------------

def bridge_module():
    spec = importlib.util.spec_from_file_location("imprint_test_route_bridge", ROOT / "hooks" / "_bridge.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridged(tmp_path, monkeypatch):
    """A bridge whose child is faked, reading this test's config."""
    monkeypatch.setenv("IMPRINT_CONFIG", str(write_config(tmp_path)))
    return bridge_module()


def child_returns(bridge, monkeypatch, *, stdout, code=0):
    monkeypatch.setattr(
        bridge, "_graceful_run", lambda argv, payload: subprocess.CompletedProcess(argv, code, stdout, ""),
    )


def child_raises(bridge, monkeypatch, exception):
    def run(argv, payload):
        raise exception

    monkeypatch.setattr(bridge, "_graceful_run", run)


def run_bridge(bridge, monkeypatch, action=SUBMIT, event="{}"):
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(event))
    return bridge.run(action)


def entry_for(outcome="delivered", byte_count=7) -> dict:
    return {
        "answered": True, "bytes": byte_count, "detail": "picked", "method": "jev", "mode": "jev",
        "outcome": outcome, "route": "user_prompt_submit", "ts": "2026-10-30T00:00:00Z",
    }


def test_the_bridge_writes_the_hook_s_line_and_keeps_it_from_the_host(tmp_path, bridged, monkeypatch, capsys):
    child = {"status": "delivered", "hookSpecificOutput": {"additionalContext": "payload"}}
    child_returns(bridged, monkeypatch, stdout=json.dumps({**child, "_imprint_route": entry_for()}))
    assert run_bridge(bridged, monkeypatch) == 0
    assert json.loads(capsys.readouterr().out) == child
    assert route_lines(tmp_path) == [entry_for()]


def test_a_line_is_written_before_the_delivery_commit(tmp_path, bridged, monkeypatch, capsys):
    seen = []

    def run(argv, payload):
        if "delivery-commit" in argv:
            seen.extend(route_lines(tmp_path))
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "status": "delivered", "_imprint_delivery": {"session_id": "s"}, "_imprint_route": entry_for(),
        }), "")

    monkeypatch.setattr(bridged, "_graceful_run", run)
    assert run_bridge(bridged, monkeypatch) == 0
    assert seen == [entry_for()]
    assert route_lines(tmp_path) == [entry_for()]


@pytest.mark.parametrize("exception, error", [
    (subprocess.TimeoutExpired("imprint", 10), "hook_action_timeout"),
    (FileNotFoundError("synthetic"), "hook_executable_unavailable"),
], ids=["timeout", "unavailable"])
def test_a_child_that_never_finished_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys, exception, error):
    child_raises(bridged, monkeypatch, exception)
    assert run_bridge(bridged, monkeypatch) == 0
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"], line["method"], line["mode"]) == ("failed", error, None, "deterministic")


def test_a_child_that_failed_is_one_failed_line_whatever_it_said(tmp_path, bridged, monkeypatch, capsys):
    child_returns(bridged, monkeypatch, stdout=json.dumps({"_imprint_route": entry_for()}), code=2)
    assert run_bridge(bridged, monkeypatch) == 0
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_action_failed")


def test_output_the_host_never_got_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys):
    child_returns(bridged, monkeypatch, stdout="not json")
    assert run_bridge(bridged, monkeypatch) == 0
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_output_invalid")


def test_input_that_cannot_be_read_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys):
    assert run_bridge(bridged, monkeypatch, event="{not json") == 0
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_input_invalid")


@pytest.mark.parametrize("stdout", ["", '{"status": "delivered"}', "[]"], ids=["silent", "no_line", "not_an_object"])
def test_a_child_that_reports_no_line_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys, stdout):
    child_returns(bridged, monkeypatch, stdout=stdout)
    assert run_bridge(bridged, monkeypatch) == 0
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_route_unreported")


def test_output_that_cannot_reach_the_host_is_one_failed_line(tmp_path, bridged, monkeypatch, capsys):
    child_returns(bridged, monkeypatch, stdout=json.dumps({"_imprint_route": entry_for()}))
    monkeypatch.setattr(bridged.sys, "stdin", io.StringIO("{}"))

    class Broken(io.StringIO):
        def write(self, value):
            raise BrokenPipeError("synthetic")

    monkeypatch.setattr(bridged.sys, "stdout", Broken())
    with pytest.raises(BrokenPipeError):
        bridged.run(SUBMIT)
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "hook_route_unreported")


def test_a_bridge_that_cannot_find_its_root_still_answers_the_host(bridged, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "imprint.hook_routes", None)
    child_returns(bridged, monkeypatch, stdout='{"status": "delivered"}')
    assert run_bridge(bridged, monkeypatch) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "delivered"}


@pytest.mark.parametrize("action", ["stop-capture", "health-check"])
def test_the_bridge_writes_no_line_for_a_hook_that_delivers_no_memory(tmp_path, bridged, monkeypatch, capsys, action):
    child_returns(bridged, monkeypatch, stdout='{"status": "skipped"}')
    assert run_bridge(bridged, monkeypatch, action) == 0
    assert route_lines(tmp_path) == []


def test_a_hook_under_the_bridge_hands_its_line_over(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(routes.ROUTE_DEFER_ENV, "1")
    body_seen = hook(write_config(tmp_path), SUBMIT, IN_DOMAIN, monkeypatch, capsys)
    assert body_seen["_imprint_route"]["outcome"] == "delivered"
    assert route_lines(tmp_path) == []


def test_a_hook_under_the_bridge_leaves_a_failure_to_the_bridge(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(routes.ROUTE_DEFER_ENV, "1")
    config = write_config(tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_payload", failing(ImprintError("synthetic")))
    assert run_expecting(config, START, {"session_id": "s"}, monkeypatch) == 2
    assert route_lines(tmp_path) == []


def test_a_hook_on_its_own_prints_no_private_field(tmp_path, monkeypatch, capsys):
    assert "_imprint_route" not in hook(write_config(tmp_path), SUBMIT, IN_DOMAIN, monkeypatch, capsys)


def test_a_real_bridge_run_writes_one_line(tmp_path):
    config = write_config(tmp_path)
    environment = {**os.environ, "IMPRINT_CONFIG": str(config)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "session_start.py")], input='{"session_id": "s"}',
        capture_output=True, text=True, env=environment, check=True,
    )
    output = json.loads(result.stdout)
    assert not any(key.startswith("_imprint") for key in output)
    (line,) = route_lines(tmp_path)
    # A real child reads the real, empty store.
    assert (line["outcome"], line["route"]) == ("nothing_relevant", "session_start")
