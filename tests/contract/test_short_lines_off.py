"""With both switches off, delivery is byte for byte what it was before short lines.

This file imports nothing that short lines added, on purpose: it has to run
unchanged against the commit before them, which is where its digests came from.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

import imprint.cli as cli
import imprint.retrieve as retrieval
from imprint.config import DEFAULTS, parse_config
from imprint.retrieve import RetrievalEngine, _jev_response, jev, retrieve_payload, select_prompt_payload
from imprint.retrieve.jev import QUESTION_PREFIX
from imprint.retrieve.models import RetrievalConfig, RetrievalRecord

KEY_ENV = "JEV_TEST_KEY"
PROMPT_SWITCH = "org.imprint.short_lines.prompt"
SESSION_START_SWITCH = "org.imprint.short_lines.session_start"
LONG = "Some background about the project and how it came about. " * 12


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
    record(1, "Believe the failed source.", "core", ontology_type="Belief", provenance_status="ratified",
           authority_tier="ratified_knowledge", case_ids=(), case_summaries=(), ontology_path=("judgment", "Belief")),
    record(2, "Never use the vendor template."),
    record(3, f"No, that is wrong. {LONG}Keep the label."),
    record(4, "Report the failed source.", "domain", domain_id="research"),
    record(5, f"The headline looks good. {LONG}But the table is wrong.", "domain", domain_id="research"),
    record(6, "An imported floor.", provenance_status="extracted", authority_tier="imported_floor",
           imported_selected=True),
]
LOCAL = record(7, "A declared offer.", "domain", domain_id="research", ontology_partition="business_declared",
               ontology_type="Offer", judgment_statement=None)
PICKS = [(record(10 + index, f"Rule {index}. {LONG * (index % 3)}"), round(0.9 - index / 100, 4)) for index in range(9)]
OWED = frozenset({PICKS[3][0].record_id})
RESERVED = {PICKS[0][0].record_id: ["Rule 0."]}
RESEARCH = [{"domain_id": "research", "public_label": "Research"}]


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Source:
    def __init__(self, records):
        self.records = records

    def retrieval_candidates(self, snapshot_id):
        return tuple(self.records)


class FakeStore:
    def snapshot(self):
        return {"generation": 1}


class Transport:
    error: Exception | None = None

    def __call__(self, endpoint, body, headers, timeout):
        if self.error is not None:
            raise self.error
        questions = json.loads(body)["questions"]
        scores = {name: 0.9 - int(name[1:]) / 100 for name in questions}
        assert all(question["instructions"].startswith(QUESTION_PREFIX) for question in questions.values())
        return json.dumps({"answers": {
            name: {"type": "noul", "noul": score} for name, score in scores.items()
        }}).encode("utf-8")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")
    monkeypatch.setattr(retrieval, "StoreRetrievalSource", lambda store: Source(RECORDS))
    monkeypatch.setattr(jev, "http_post", Transport())


def selector(mode: str = "jev") -> dict:
    value = json.loads(json.dumps(DEFAULTS["selector"]))
    value["mode"] = mode
    value["jev"]["api_key_env"] = KEY_ENV
    return value


def write_config(tmp_path: Path, name: str, **values) -> Path:
    (tmp_path / name).mkdir()
    path = tmp_path / name / "config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / name / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True, "context_budget_bytes": 4096, "domains": RESEARCH, **values,
    }))
    return path


def hook(config: Path, action: str, event: dict, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    assert cli.main(["--config", str(config), "hook", action]) == 0
    return json.loads(capsys.readouterr().out)


def test_session_start_is_unchanged(tmp_path):
    response = retrieve_payload(FakeStore(), root=tmp_path, session_id="s", budget=4096, refresh=True)
    assert digest(response) == "d9339cbb2411bd5afdbcddc848a45ef548051c9413f9c394758bd6cd2a35a578"


def test_a_larger_session_start_is_unchanged(tmp_path):
    assert digest(retrieve_payload(FakeStore(), root=tmp_path, session_id="s", refresh=True)) == "261c30cf3abbd881aaeea9df607e7e8d8953db93b01c3eaccf57b0975d9a963f"


def test_a_matched_domain_is_unchanged(tmp_path):
    response = retrieve_payload(
        FakeStore(), root=tmp_path, session_id="s", budget=4096, refresh=True, explicit_domain="research",
        domain_only=True,
    )
    assert digest(response) == "b9ba4fc38ad12538467b7527a424e7d09af08b3b42f26bbf7f283e902c611eb7"


def test_picks_are_unchanged():
    response = _jev_response("snapshot", "scope", PICKS, "picked", 12, 4096, None, OWED, RESERVED, 2)
    assert digest(response) == "6c574b57c2982214f371f86354af5fa703fb2ec0fa0baedb010d38fd53f76f49"


def test_picks_beside_local_records_are_unchanged():
    local = RetrievalEngine(Source([LOCAL]), RetrievalConfig(total_budget_bytes=4096)).retrieve(
        snapshot_id="snapshot", query="turn", selected_domain="research",
    )
    response = _jev_response("snapshot", "scope", PICKS[:4], "picked", 5, 4096, local)
    assert digest(response) == "1f6717bf1bdec0515e4dc136c502d268bd5b44bf69223bd120e9949448607289"


def test_a_full_selector_turn_is_unchanged(tmp_path):
    decision = select_prompt_payload(
        FakeStore(), root=tmp_path, session_id="s", state="the turn", explicit_domain="research",
        budget=4096, selector=selector(), mode="jev",
    )
    assert digest(decision.response) == "69eef886c5bdd193685d295307180f0d72658b8f195d48ad18278f5c374e8fec"


def test_a_hook_session_start_is_unchanged(tmp_path, monkeypatch, capsys):
    body = hook(write_config(tmp_path, "c"), "session-start", {"session_id": "s"}, monkeypatch, capsys)
    assert digest(body) == "2c26bd97d5ad31f9f66afa402d48e51caebca228a48a732b203c77e7bd6e95b2"


def test_a_hook_domain_prompt_is_unchanged(tmp_path, monkeypatch, capsys):
    event = {"session_id": "s", "prompt": "go", "domain_id": "research"}
    body = hook(write_config(tmp_path, "c"), "user-prompt-submit", event, monkeypatch, capsys)
    assert digest(body) == "384b3da1ef2e0a6320201801d9089571e4539af1f51cc8fcb9b5078065b773bb"


def test_a_hook_selector_prompt_is_unchanged(tmp_path, monkeypatch, capsys):
    event = {"session_id": "s", "prompt": "go", "domain_id": "research"}
    body = hook(write_config(tmp_path, "c", selector=selector()), "user-prompt-submit", event, monkeypatch, capsys)
    assert digest(body) == "673994926c2e08a65977518416d1d166f8c11111452405436d50936e5dc459bf"


def test_a_hook_fallback_is_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Transport, "error", TimeoutError("synthetic timeout"))
    event = {"session_id": "s", "prompt": "go", "domain_id": "research"}
    body = hook(write_config(tmp_path, "c", selector=selector()), "user-prompt-submit", event, monkeypatch, capsys)
    assert body["selector_method"] == "deterministic_fallback"
    assert digest(body) == "39e6d6957a6c1cd81ba33bc0b240bd2bfb9ca4622179948e233d5bba7b3b3dfe"


def test_explicit_retrieval_is_unchanged(tmp_path, capsys):
    config = write_config(tmp_path, "c")
    assert cli.main(["--config", str(config), "retrieve", "--session", "by-hand", "--refresh"]) == 0
    assert digest(json.loads(capsys.readouterr().out)["payload"]) == "9a0126ce0655863d7a16a38fdae5c28e069ee78d75f59b9ed9b12a36a82394ca"


@pytest.mark.parametrize("off", [{}, {PROMPT_SWITCH: False, SESSION_START_SWITCH: False}], ids=["absent", "false"])
def test_a_switch_set_to_false_is_the_same_as_no_switch(tmp_path, monkeypatch, capsys, off):
    config = write_config(tmp_path, "c", selector=selector(), **off)
    start = hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys)
    event = {"session_id": "s", "prompt": "go", "domain_id": "research"}
    prompt = hook(config, "user-prompt-submit", event, monkeypatch, capsys)
    assert digest(start) == "2c26bd97d5ad31f9f66afa402d48e51caebca228a48a732b203c77e7bd6e95b2"
    assert digest(prompt) == "7f71b26c7eef9bdaf2fd4491a09469309122518598a57d5661e98fbad81db914"


def test_a_build_without_the_switches_accepts_a_config_that_sets_them():
    config = parse_config({PROMPT_SWITCH: True, SESSION_START_SWITCH: True})
    assert (config[PROMPT_SWITCH], config[SESSION_START_SWITCH]) == (True, True)
