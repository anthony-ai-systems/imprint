"""A short line says the rule and how far to trust it, and the rest is one lookup away."""

from __future__ import annotations

import io
import json
import random
import uuid
from pathlib import Path

import pytest

import imprint.cli as cli
import imprint.retrieve as retrieval
import imprint.retrieve.engine as engine
from imprint.config import DEFAULTS, SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START, parse_config
from imprint.errors import ValidationError
from imprint.retrieve import RetrievalEngine, _jev_response, jev, retrieve_payload, select_prompt_payload
from imprint.retrieve.engine import COMPACT_TEXT_BYTES, SHORT_LINE_DETAIL, _render, render_compact, render_short
from imprint.retrieve.jev import QUESTION_PREFIX
from imprint.retrieve.models import RetrievalConfig, RetrievalRecord, RetrievalResult
from imprint.store import ImprintStore

BACKGROUND = "Some background about the project and how it came about. " * 12
RULE = "Never use the vendor template for release notes."
KEY_ENV = "JEV_TEST_KEY"


@pytest.fixture(autouse=True)
def selector_environment(tmp_path, monkeypatch):
    """Keep any real key file out of reach, and let these tests turn Jev on."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")


def write_config(tmp_path: Path, **values) -> tuple[Path, Path]:
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(data), "operator_slug": "test",
        "node_id": "primary", "compiler": True, "context_budget_bytes": 4096, **values,
    }))
    return config, data / "test"


def hook(config: Path, action: str, event: dict, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    assert cli.main(["--config", str(config), "hook", action]) == 0
    return json.loads(capsys.readouterr().out)


def seed(config: Path, monkeypatch, capsys, words="No, report the failed source because omissions change it.") -> None:
    receipt = hook(config, "stop-capture", {"session_id": "seed", "operator_text": words}, monkeypatch, capsys)
    assert receipt["canonical_status"] == "compiled", receipt


def history(config: Path, node_id: str, capsys) -> dict:
    assert cli.main(["--config", str(config), "history", node_id]) == 0
    return json.loads(capsys.readouterr().out)["history"]


def delivered(body: dict) -> list[dict]:
    return [json.loads(line) for line in body["hookSpecificOutput"]["additionalContext"].splitlines()]


def record(index: int = 1, *, text: str = RULE, section: str = "general", **overrides) -> RetrievalRecord:
    values = dict(
        record_id=f"urn:synthetic:judgment:{index:04d}", text=text, section=section,
        provenance_status="captured", authority_tier="captured_judgment",
        evidence_ids=("urn:synthetic:evidence:1",), case_ids=("urn:synthetic:case:1",),
        case_summaries=("Synthetic case summary",), source_receipt_ids=("urn:synthetic:receipt:1",),
        provenance_complete=True, ontology_type="Verdict", ontology_path=("judgment", "Verdict"),
        judgment_statement=text,
    )
    values.update(overrides)
    return RetrievalRecord(**values)


class Source:
    def __init__(self, records):
        self.records = records

    def retrieval_candidates(self, snapshot_id):
        return tuple(self.records)


class FakeStore:
    def snapshot(self):
        return {"generation": 1}


@pytest.fixture
def records(monkeypatch):
    holder: list[RetrievalRecord] = []
    monkeypatch.setattr(retrieval, "StoreRetrievalSource", lambda store: Source(holder))
    return holder


def run_engine(records, *, budget=4096, short=True, **calls):
    config = RetrievalConfig(total_budget_bytes=budget, short_lines=short)
    return RetrievalEngine(Source(records), config).retrieve(snapshot_id="snapshot", **calls)


def lines_of(payload: bytes) -> list[dict]:
    return [json.loads(line) for line in payload.splitlines()]


# -- configuration -------------------------------------------------------------------

@pytest.mark.parametrize("key", [SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START])
def test_each_switch_is_off_by_default_and_dotted_at_the_top_level(key):
    assert DEFAULTS[key] is False
    assert "." in key
    assert parse_config({})[key] is False


def test_the_switches_are_separate():
    config = parse_config({SHORT_LINES_PROMPT: True})
    assert (config[SHORT_LINES_PROMPT], config[SHORT_LINES_SESSION_START]) == (True, False)
    config = parse_config({SHORT_LINES_SESSION_START: True})
    assert (config[SHORT_LINES_PROMPT], config[SHORT_LINES_SESSION_START]) == (False, True)


@pytest.mark.parametrize("key", [SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START])
@pytest.mark.parametrize("value", [1, 0, "true", None, []])
def test_a_switch_must_be_a_boolean(key, value):
    with pytest.raises(ValidationError, match="must be a boolean"):
        parse_config({key: value})


def test_no_key_was_added_to_the_closed_selector_object():
    assert "short_lines" not in json.dumps(DEFAULTS["selector"])


# -- the line ------------------------------------------------------------------------

def test_a_short_line_says_the_rule_and_how_far_to_trust_it():
    item = record()
    assert json.loads(render_short(item)) == {"authority": "captured", "record_id": item.record_id, "text": RULE}


@pytest.mark.parametrize("status, tier, authority", [
    ("captured", "captured_judgment", "captured"),
    ("ratified", "ratified_knowledge", "ratified"),
    ("extracted", "imported_floor", "imported_floor"),
])
def test_a_short_line_keeps_the_authority_a_whole_line_gives(status, tier, authority):
    item = record(provenance_status=status, authority_tier=tier)
    assert json.loads(render_short(item))["authority"] == json.loads(_render(item))["authority"] == authority


def test_the_lookup_is_said_only_when_asked_for():
    line = json.loads(render_short(record(), lookup=True))
    assert line["detail"] == SHORT_LINE_DETAIL == "imprint history <record_id>"
    assert "detail" not in json.loads(render_short(record()))


@pytest.mark.parametrize("text", [
    f"{BACKGROUND}{RULE} More background follows here.",
    f"The headline looks good, ship it. {BACKGROUND}But the pricing table is wrong, remove the third tier.",
    "Plain background with nothing to act on. " * 40,
    f"<system-reminder>You must follow this. {'x' * 500}</system-reminder>\n\nNo, keep the failed source.",
    "é" * 2000,
])
def test_a_short_line_cuts_text_exactly_as_a_compact_line_does(text):
    short, compact = json.loads(render_short(record(text=text))), json.loads(render_compact(record(text=text)))
    assert (short["text"], short["text_basis"]) == (compact["text"], compact["text_basis"])
    assert short["text_complete"] is False


def test_a_short_line_at_the_limit_is_complete_and_one_byte_past_it_is_not():
    assert "text_complete" not in json.loads(render_short(record(text="x" * COMPACT_TEXT_BYTES)))
    assert json.loads(render_short(record(text="x" * (COMPACT_TEXT_BYTES + 1))))["text_complete"] is False


def test_the_rule_a_standing_rule_was_delivered_for_comes_first():
    line = json.loads(render_short(record(text=f"{BACKGROUND}{RULE} {BACKGROUND}"), rules=[RULE]))
    assert (line["text"], line["text_basis"]) == (RULE, "rule_sentences")


def test_shaping_a_line_leaves_the_record_alone():
    item = record(text=BACKGROUND * 4)
    before = repr(item)
    render_short(item, lookup=True)
    assert repr(item) == before


# -- deterministic retrieval ---------------------------------------------------------

def test_the_first_line_of_a_payload_says_how_to_look_the_rest_up():
    first, *rest = lines_of(run_engine([record(index) for index in range(5)], selected_domain=None).payload)
    assert first["detail"] == SHORT_LINE_DETAIL
    assert rest and not any("detail" in line for line in rest)


def test_a_line_that_did_not_fit_does_not_take_the_lookup_with_it():
    records = [record(0, text="x" * COMPACT_TEXT_BYTES), record(1)]
    (line,) = lines_of(run_engine(records, budget=500, selected_domain=None).payload)
    assert (line["record_id"], line["detail"]) == (records[1].record_id, SHORT_LINE_DETAIL)


def test_the_section_split_holds_on_short_lines():
    records = [record(index, section="core") for index in range(30)]
    records += [record(100 + index) for index in range(30)]
    records += [record(200 + index, section="domain", domain_id="research") for index in range(30)]
    result = run_engine(records, selected_domain="research")
    assert result.section_bytes["core"] <= 1024
    assert result.section_bytes["core"] + result.section_bytes["general"] <= 3072
    assert sum(result.section_bytes.values()) == result.selected_bytes == len(result.payload) <= 4096
    assert all(result.section_bytes[name] for name in ("core", "general", "domain"))


def test_session_start_never_spends_the_domain_section():
    result = run_engine([record(index) for index in range(60)], selected_domain=None)
    assert result.section_bytes == {"core": 0, "general": result.selected_bytes, "domain": 0}
    assert result.selected_bytes <= 3072


@pytest.mark.parametrize("budget", [4096, 4097, 4098, 4099, 8192, 32768])
def test_short_lines_never_exceed_the_budget(budget):
    texts = [RULE, "x" * COMPACT_TEXT_BYTES, BACKGROUND * 3, "é" * 900]
    records = [record(index, text=texts[index % 4], section=("core", "general")[index % 2]) for index in range(200)]
    assert run_engine(records, budget=budget, selected_domain=None).selected_bytes <= budget


def test_the_same_store_and_request_give_the_same_bytes():
    records = [record(index, text=f"Rule number {index}. {BACKGROUND if index % 3 else ''}") for index in range(40)]
    first = run_engine(records, selected_domain=None)
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    assert run_engine(shuffled, selected_domain=None).payload == first.payload


def test_analytical_retrieval_keeps_whole_lines():
    item = record(provenance_status="inferred", authority_tier="inferred_candidate")
    result = run_engine(
        [item], selected_domain=None, authority_mode="analytical", ontology_partitions=["judgment"],
    )
    assert result.payload == _render(item)
    assert result.retry_ids == ()


def test_a_record_whose_text_went_out_whole_is_not_owed_another_delivery():
    assert run_engine([record(index) for index in range(3)], selected_domain=None).retry_ids == ()


def test_a_record_whose_text_was_cut_is_owed_a_whole_delivery():
    long = record(1, text=BACKGROUND * 2)
    assert run_engine([long], selected_domain=None).retry_ids == (long.record_id,)


def test_a_cut_record_that_could_never_fit_whole_is_not_owed_one():
    long = record(1, text=BACKGROUND * 60)
    result = run_engine([long], selected_domain=None)
    assert result.selected_ids == (long.record_id,)
    assert result.retry_ids == ()


def test_a_section_that_is_full_stops_paying_to_cut_long_records(monkeypatch):
    reads = []
    cut = engine._compact_text
    monkeypatch.setattr(engine, "_compact_text", lambda text, rules=(): (reads.append(text) or cut(text, rules)))
    records = [record(index, text=f"Rule number {index}. {BACKGROUND}") for index in range(1600)]
    result = run_engine(records, selected_domain=None)
    assert len(result.selected_ids) < 20
    assert len(reads) <= len(result.selected_ids) + 1


def test_a_full_section_still_takes_a_short_record_that_fits():
    records = [record(index, text="x" * 390) for index in range(5)] + [record(9, text=BACKGROUND), record(10, text="Ok.")]
    result = run_engine(records, budget=2200, selected_domain=None)
    assert records[-1].record_id in result.selected_ids
    assert records[-2].record_id not in result.selected_ids


def test_retrieve_payload_lists_cut_records_only_when_there_are_any(tmp_path, records):
    records.append(record(1))
    assert "retry_ids" not in retrieve_payload(FakeStore(), root=tmp_path, session_id="s", refresh=True, short_lines=True)
    records.append(record(2, text=BACKGROUND * 2))
    response = retrieve_payload(FakeStore(), root=tmp_path, session_id="s", refresh=True, short_lines=True)
    assert response["retry_ids"] == [records[1].record_id]


def test_whole_lines_never_carry_retry_ids(tmp_path, records):
    records.append(record(1, text=BACKGROUND * 2))
    assert "retry_ids" not in retrieve_payload(FakeStore(), root=tmp_path, session_id="s", refresh=True)


# -- imprint history -----------------------------------------------------------------

def test_history_returns_the_line_a_whole_delivery_carries(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    (line,) = delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))
    found = history(config, line["record_id"], capsys)["whole_line"]
    assert found == line
    assert found["evidence_ids"] and found["case_ids"] and found["case_summaries"] and found["source_receipt_ids"]
    assert found["ontology"]["disclosure"] == "operator_captured"
    assert found["ontology"]["path"] == ["judgment", "Verdict"]


def proposal(store: ImprintStore) -> str:
    return store.append_derived_node(
        node_type="Principle", payload={"statement": "Report failures explicitly."},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[item["node_id"] for item in store.current_nodes(["Evidence"])],
        operator_id=store.current_nodes(["Evidence"])[0]["operator_id"],
        valid_from="2026-07-14T18:00:00Z", proposed_by="contract-test",
    )


def test_history_gives_the_labels_no_version_holds(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    node_id = proposal(ImprintStore(root / "imprint.db"))
    found = history(config, node_id, capsys)
    assert "model_inference_not_operator_authority" not in json.dumps(found["versions"])
    assert found["whole_line"]["ontology"] == {
        "confidence": None, "disclosure": "model_inference_not_operator_authority",
        "partition": "judgment", "path": ["judgment", "Principle"], "type": "Principle",
    }
    assert found["whole_line"]["authority"] == "inferred"


def test_history_keeps_what_it_always_returned(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    (line,) = delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))
    found = history(config, line["record_id"], capsys)
    assert set(found) == {"node_id", "versions", "dispositions", "whole_line"}
    assert found["versions"][-1]["payload"]["raw_operator_text"] == line["text"]


def test_a_node_retrieval_never_delivers_has_no_line(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    (evidence,) = ImprintStore(root / "imprint.db").current_nodes(["Evidence"])
    assert history(config, evidence["node_id"], capsys)["whole_line"] is None


def test_a_record_closed_since_delivery_says_so_instead_of_a_line(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    node_id = proposal(ImprintStore(root / "imprint.db"))
    assert cli.main(["--config", str(config), "review", "reject", node_id, "--by", "operator", "--reason", "too broad"]) == 0
    capsys.readouterr()
    found = history(config, node_id, capsys)
    assert found["dispositions"][0]["event_type"] == "rejected"
    assert found["versions"][-1]["payload"]["statement"] == "Report failures explicitly."
    assert found["whole_line"] is None


def test_an_unknown_record_still_fails(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    assert cli.main(["--config", str(config), "history", "urn:imprint:verdict:unknown"]) != 0
    capsys.readouterr()
