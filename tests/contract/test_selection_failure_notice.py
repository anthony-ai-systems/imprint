"""A failed selection can say so to the agent, and health can say how often it fails.

The notice is one fixed line behind its own switch, and with the switch off or absent
nothing a hook emits changes. Records are synthetic and every request goes to a fake.
"""

from __future__ import annotations

import copy
import io
import json
import urllib.error
from pathlib import Path

import pytest

import imprint.cli as cli
import imprint.retrieve as retrieval
from imprint.config import DEFAULTS, SELECTION_FAILURE_NOTICE, parse_config
from imprint.errors import ValidationError
from imprint.hook_routes import route_log_path
from imprint.retrieve import jev
from imprint.retrieve.models import RetrievalRecord
from imprint.selection_notice import NOTICE_REASONS, failure_notice

KEY_ENV = "JEV_TEST_KEY"
PROMPT = "Should the synthetic prompt words ever reach the notice?"
SUBMIT = "user-prompt-submit"
IN_DOMAIN = {"session_id": "s", "prompt": PROMPT, "domain_id": "research"}
NO_DOMAIN = {"session_id": "s", "prompt": PROMPT}
FAILURES = ("missing_key", "key_file_unsafe", "timeout", "http_error", "bad_response", "exception")


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
        return json.dumps({"answers": {name: {"type": "noul", "noul": self.score} for name in questions}}).encode()


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


def write_config(tmp_path: Path, mode: str = "jev", notice: bool | None = True, **jev_values) -> Path:
    path = tmp_path / "config.json"
    extra = {} if notice is None else {SELECTION_FAILURE_NOTICE: notice}
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True,
        "domains": [{"domain_id": "research", "public_label": "Research"}],
        "selector": selector(mode, **jev_values), **extra,
    }))
    return path


def operator_root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "test"


def raw_hook(config: Path, event: dict, monkeypatch, capsys) -> str:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    assert cli.main(["--config", str(config), "hook", SUBMIT]) == 0
    return capsys.readouterr().out


def hook(config: Path, event: dict, monkeypatch, capsys) -> dict:
    return json.loads(raw_hook(config, event, monkeypatch, capsys))


def context(body: dict) -> str:
    return body["hookSpecificOutput"]["additionalContext"]


def route_lines(tmp_path: Path) -> list[dict]:
    path = route_log_path(operator_root(tmp_path))
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def fail_with(monkeypatch, reason: str) -> None:
    monkeypatch.setattr(jev.JevSelector, "select", lambda self, *args, **kwargs: jev.SelectionResult(
        fallback_reason=reason,
    ))


# -- the line itself ---------------------------------------------------------------

def test_the_notice_names_each_failure_and_nothing_else():
    assert NOTICE_REASONS == frozenset(FAILURES)
    for reason in FAILURES:
        assert failure_notice(reason) == (
            f"Imprint: judgment retrieval was unavailable this turn (reason: {reason}).\n"
        )


def test_the_notice_covers_every_selector_failure_and_no_empty_pool():
    # A reason the selector adds later has to be added here too, or it would go unnamed.
    assert NOTICE_REASONS == jev.FALLBACK_REASONS - {"no_candidates"}
    assert failure_notice("no_candidates") is None
    for value in (None, "", "picked", "no_picks", "t3_scratch_thread", "oversized_picks", 3):
        assert failure_notice(value) is None


def test_the_switch_is_off_by_default_and_must_be_a_boolean():
    assert DEFAULTS[SELECTION_FAILURE_NOTICE] is False
    assert parse_config({})[SELECTION_FAILURE_NOTICE] is False
    for value in ("true", 1, None, {"enabled": True}):
        with pytest.raises(ValidationError, match=SELECTION_FAILURE_NOTICE):
            parse_config({SELECTION_FAILURE_NOTICE: value})


# -- on --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", FAILURES)
def test_each_failed_selection_with_nothing_delivered_says_so(tmp_path, monkeypatch, capsys, reason):
    fail_with(monkeypatch, reason)
    body = hook(write_config(tmp_path), NO_DOMAIN, monkeypatch, capsys)
    assert (body["selector_method"], body["fallback_reason"]) == ("deterministic_fallback", reason)
    assert context(body) == failure_notice(reason)
    assert body["selector_notice"] == "selection_failed"
    assert PROMPT not in json.dumps(body)
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"], line["notice"]) == ("failed", reason, True)
    assert line["bytes"] == len(failure_notice(reason).encode("utf-8"))


def test_a_real_transport_error_reads_http_error(tmp_path, transport, monkeypatch, capsys):
    transport.error = urllib.error.URLError("synthetic refusal")
    body = hook(write_config(tmp_path), NO_DOMAIN, monkeypatch, capsys)
    assert context(body) == failure_notice("http_error")
    assert transport.calls == 1


def test_a_malformed_reply_reads_bad_response(tmp_path, transport, monkeypatch, capsys):
    transport.raw = b"not json"
    body = hook(write_config(tmp_path), NO_DOMAIN, monkeypatch, capsys)
    assert context(body) == failure_notice("bad_response")


def test_a_missing_key_reads_missing_key(tmp_path, transport, monkeypatch, capsys):
    monkeypatch.delenv(KEY_ENV)
    body = hook(write_config(tmp_path), NO_DOMAIN, monkeypatch, capsys)
    assert context(body) == failure_notice("missing_key")
    assert transport.calls == 0


def test_local_domain_records_delivered_anyway_carry_no_notice(tmp_path, monkeypatch, capsys):
    fail_with(monkeypatch, "timeout")
    body = hook(write_config(tmp_path), IN_DOMAIN, monkeypatch, capsys)
    assert body["fallback_reason"] == "timeout"
    assert "research domain" in context(body)
    assert "unavailable" not in context(body)
    assert "selector_notice" not in body
    (line,) = route_lines(tmp_path)
    assert (line["outcome"], line["detail"]) == ("failed", "timeout")
    assert "notice" not in line


def test_a_domain_already_delivered_this_session_still_gets_the_notice(tmp_path, transport, monkeypatch, capsys):
    config = write_config(tmp_path)
    transport.error = urllib.error.URLError("synthetic refusal")
    first = hook(config, IN_DOMAIN, monkeypatch, capsys)
    assert "research domain" in context(first)
    second = hook(config, {**IN_DOMAIN, "prompt": "another synthetic prompt"}, monkeypatch, capsys)
    assert context(second) == failure_notice("http_error")


def test_an_empty_pool_is_not_a_failure(tmp_path, transport, monkeypatch, capsys):
    use_records(monkeypatch, [])
    body = hook(write_config(tmp_path), NO_DOMAIN, monkeypatch, capsys)
    assert body["fallback_reason"] == "no_candidates"
    assert context(body) == "" and "selector_notice" not in body


def test_an_answer_with_no_picks_is_not_a_failure(tmp_path, transport, monkeypatch, capsys):
    transport.score = 0.05
    body = hook(write_config(tmp_path), NO_DOMAIN, monkeypatch, capsys)
    assert body["selector_outcome"] == "no_picks"
    assert context(body) == "" and "selector_notice" not in body


def test_shadow_delivers_the_deterministic_answer_with_no_notice(tmp_path, monkeypatch, capsys):
    fail_with(monkeypatch, "timeout")
    body = hook(write_config(tmp_path, "shadow"), NO_DOMAIN, monkeypatch, capsys)
    assert (body["selector_method"], body["fallback_reason"]) == ("shadow", "timeout")
    assert context(body) == "" and "selector_notice" not in body


def test_a_deterministic_store_never_gets_the_notice(tmp_path, transport, monkeypatch, capsys):
    body = hook(write_config(tmp_path, "deterministic"), NO_DOMAIN, monkeypatch, capsys)
    assert context(body) == "" and "selector_notice" not in body
    assert transport.calls == 0


def test_a_kill_switch_turn_never_gets_the_notice(tmp_path, transport, monkeypatch, capsys):
    monkeypatch.setenv("IMPRINT_SELECTOR", "off")
    body = hook(write_config(tmp_path), NO_DOMAIN, monkeypatch, capsys)
    assert context(body) == "" and "selector_notice" not in body


# -- off -------------------------------------------------------------------------

@pytest.mark.parametrize("reason", FAILURES)
@pytest.mark.parametrize("event", [NO_DOMAIN, IN_DOMAIN], ids=["no_domain", "domain"])
def test_off_or_absent_is_byte_identical(tmp_path, monkeypatch, capsys, reason, event):
    fail_with(monkeypatch, reason)
    outputs, lines = [], []
    for notice in (None, False):
        home = tmp_path / str(notice)
        home.mkdir()
        outputs.append(raw_hook(write_config(home, notice=notice), event, monkeypatch, capsys))
        lines.append([{k: v for k, v in line.items() if k != "ts"} for line in route_lines(home)])
    assert outputs[0] == outputs[1]
    assert lines[0] == lines[1]
    body = json.loads(outputs[0])
    assert "selector_notice" not in body and "notice" not in lines[0][0]
    if event is NO_DOMAIN:
        assert context(body) == ""
