"""A short line says the rule and how far to trust it, and the rest is one lookup away."""

from __future__ import annotations

import io
import json
import os
import random
import shlex
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

import pytest

import imprint.cli as cli
import imprint.retrieve as retrieval
import imprint.retrieve.engine as engine
from imprint.capture.schema import build_capture_envelope, new_urn
from imprint.config import DEFAULTS, SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START, parse_config
from imprint.errors import ValidationError
from imprint.retrieve import RetrievalEngine, _jev_response, jev, retrieve_payload, select_prompt_payload, whole_line
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


def labelled(partition: str, node_type: str, text: str) -> RetrievalRecord:
    return record(text=text, provenance_status="ratified", authority_tier="ratified_knowledge",
                  ontology_partition=partition, ontology_type=node_type, judgment_statement=None)


@pytest.mark.parametrize("partition, node_type", [
    ("business_declared", "Claim"), ("business_observed", "Result"), ("self_model", "SelfModelAssertion"),
    ("chosen_future", "ChosenFuture"), ("default_future", "DefaultFuture"),
    ("direction_comparison", "DirectionComparison"),
])
def test_a_short_line_says_a_partition_that_is_not_judgment(partition, node_type):
    item = labelled(partition, node_type, RULE)
    line = json.loads(render_short(item))
    assert line == {"authority": "ratified", "partition": partition, "record_id": item.record_id, "text": RULE}
    assert line["partition"] == json.loads(_render(item))["ontology"]["partition"]


def test_declared_theory_and_observed_evidence_stay_apart_on_short_lines():
    declared = labelled("business_declared", "Claim", "Onboarding calls halve churn inside 90 days.")
    observed = labelled("business_observed", "Result", "Onboarding calls halved churn inside 90 days.")
    assert json.loads(render_short(declared))["partition"] != json.loads(render_short(observed))["partition"]


def test_a_judgment_pays_nothing_for_the_partition_label():
    item = record()
    assert "partition" not in json.loads(render_short(item))
    assert len(render_short(item)) == len(render_short(record()))


def test_the_label_costs_a_line_at_most_35_bytes():
    plain = record()
    assert max(
        len(render_short(labelled(partition, "Claim", RULE))) - len(render_short(plain))
        for partition in ("business_declared", "business_observed", "direction_comparison")
    ) == 35


def test_the_lookup_is_said_only_when_asked_for():
    line = json.loads(render_short(record(), lookup=True))
    assert line["detail"] == SHORT_LINE_DETAIL == "imprint details <record_id>"
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
    assert line["text"].split("\n")[0] == RULE
    assert line["text_basis"] == "rule_sentences"


@pytest.mark.parametrize("rule, closing", [
    ("Always use the vendor template for release notes.",
     "Scratch that first part, it was wrong and the vendor template must go."),
    ("Never email the client directly.", "The one exception is the billing contact."),
])
def test_a_standing_rule_keeps_the_sentence_that_overrules_it(rule, closing):
    line = json.loads(render_short(record(text=f"{rule} {BACKGROUND}{closing}"), rules=[rule]))
    assert line["text"] == f"{rule}\n{closing}"
    assert (line["text_basis"], line["text_complete"]) == ("rule_sentences", False)


def test_a_long_closing_sentence_leaves_the_rule_its_half():
    rule, closing = f"Never {'use the vendor template ' * 13}again.", f"Except {'when the client asks in writing ' * 12}first."
    text = json.loads(render_short(record(text=f"{rule} {BACKGROUND}{closing}"), rules=[rule]))["text"]
    said, end = text.split("\n")
    assert rule.startswith(said) and closing.startswith(end)
    assert len(said) > COMPACT_TEXT_BYTES // 3 and len(end) > COMPACT_TEXT_BYTES // 3
    assert len(text.encode("utf-8")) <= COMPACT_TEXT_BYTES


def test_a_rule_that_is_the_last_sentence_is_said_once():
    item = record(text=f"{BACKGROUND}{RULE}")
    assert json.loads(render_short(item, rules=[RULE]))["text"] == RULE


def test_a_compact_line_still_says_its_rule_and_nothing_else():
    item = record(text=f"{RULE} {BACKGROUND}Scratch that first part, it was wrong.")
    assert json.loads(render_compact(item, rules=[RULE]))["text"] == RULE


HANDBOOK = "\n".join((
    "Refund policy, quoted from the handbook:",
    *["> Refunds are never issued after thirty days from the date of purchase."] * 6,
    "This does not apply to annual plans.",
))


def imported(text: str, **overrides) -> RetrievalRecord:
    values = dict(
        text=text, provenance_status="extracted", authority_tier="imported_floor", imported_selected=True,
        ontology_type="IngestedItem", ontology_path=("judgment", "IngestedItem"), judgment_statement=None,
    )
    return record(**{**values, **overrides})


def test_a_record_no_operator_typed_keeps_what_it_quotes():
    line = json.loads(render_short(imported(HANDBOOK)))
    assert "> Refunds are never issued after thirty days" in line["text"]
    assert line["text"].endswith("\nThis does not apply to annual plans.")
    assert (line["text_basis"], line["text_complete"]) == ("stored_text", False)
    assert len(line["text"].encode("utf-8")) <= COMPACT_TEXT_BYTES


def test_a_record_no_operator_typed_ends_as_it_is_stored():
    text = f"{BACKGROUND}\n> The closing clause is a quotation and it is the one that matters."
    assert json.loads(render_short(imported(text)))["text"].endswith("\n> The closing clause is a quotation and it is the one that matters.")


def test_ratified_text_is_read_as_it_is_stored_too():
    item = imported(HANDBOOK, provenance_status="ratified", authority_tier="ratified_knowledge", imported_selected=False)
    assert "> Refunds are never issued" in json.loads(render_short(item))["text"]


def test_a_captured_message_still_loses_what_it_quotes():
    text = f"Keep the summary short and say what failed.\n{HANDBOOK}\nNo, the refund note must go."
    item = record(text=text)
    line = json.loads(render_short(item))
    assert "> Refunds" not in line["text"]
    assert (line["text"], line["text_basis"]) == (
        json.loads(render_compact(item))["text"], json.loads(render_compact(item))["text_basis"],
    )


REPEATED = "Use it for every release and every client. " * 12
VERDICT = "No, never use the vendor template for release notes."
OVERRULED = f'They wrote: "always prefer the vendor template. {REPEATED}" {VERDICT}'


def test_a_long_sentence_keeps_the_verdict_a_quotation_would_push_out():
    line = json.loads(render_short(record(text=OVERRULED)))
    assert line["text"].endswith(f"\n{VERDICT}") and line["text"].startswith("They wrote:")
    assert (line["text_basis"], line["text_complete"]) == ("feedback_sentences", False)
    assert len(line["text"].encode("utf-8")) <= COMPACT_TEXT_BYTES


def test_a_long_standing_rule_keeps_the_verdict_too():
    line = json.loads(render_short(record(text=OVERRULED), rules=[OVERRULED]))
    assert line["text"].endswith(f"\n{VERDICT}")
    assert (line["text_basis"], line["text_complete"]) == ("rule_sentences", False)
    assert len(line["text"].encode("utf-8")) <= COMPACT_TEXT_BYTES


def test_the_ending_comes_from_the_sentence_and_not_from_what_was_pasted_after_it():
    text = f"{OVERRULED}\n> Always use the vendor template, the handbook says."
    line = json.loads(render_short(record(text=text)))
    assert line["text"].endswith(f"\n{VERDICT}") and "> Always" not in line["text"]


def test_a_compact_line_still_cuts_a_long_sentence_from_its_start():
    line = json.loads(render_compact(record(text=OVERRULED)))
    assert line["text"].startswith("They wrote:") and VERDICT not in line["text"]


def test_a_sentence_with_no_ending_of_its_own_is_cut_from_its_start():
    text = f"Never use {'the vendor template ' * 40}again."
    short, compact = json.loads(render_short(record(text=text))), json.loads(render_compact(record(text=text)))
    assert short["text"] == compact["text"]


def test_a_record_of_nothing_but_leading_blanks_is_never_an_empty_line():
    assert json.loads(render_short(imported(f"{' ' * 500}Refunds end after thirty days. {BACKGROUND}")))["text"].strip()


def test_an_imported_standing_rule_keeps_how_its_record_ended():
    rule = "Never refund after thirty days."
    text = f"{rule} {BACKGROUND}\n> The one exception is an annual plan."
    assert json.loads(render_short(imported(text), rules=[rule]))["text"] == f"{rule}\n> The one exception is an annual plan."


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


def test_a_cut_record_too_large_for_this_budget_is_still_listed():
    long = record(1, text=BACKGROUND * 60)
    result = run_engine([long], selected_domain=None)
    assert result.selected_ids == (long.record_id,)
    assert result.retry_ids == (long.record_id,)


def test_a_section_that_is_full_stops_paying_to_cut_long_records(monkeypatch):
    reads = []
    cut = engine._compact_text
    monkeypatch.setattr(engine, "_compact_text", lambda text, rules=(), **how: (reads.append(text) or cut(text, rules, **how)))
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


# -- the selector route --------------------------------------------------------------

def pick(index: int = 1, **values) -> tuple[RetrievalRecord, float]:
    return record(index, **values), round(0.9 - index / 1000, 4)


def respond(picks, *, budget=4096, local=None, retried=frozenset(), reserved=None):
    return _jev_response(
        "snapshot", "scope", picks, "picked", len(picks), budget, local, retried, reserved, 0, True,
    )


# Enough plain picks behind a long one that its whole line has no room beside them.
CROWD = [pick(index) for index in range(100, 140)]


def lines(response: dict) -> list[dict]:
    return [json.loads(line) for line in response["payload"].splitlines()]


def local_result(*records_) -> RetrievalResult:
    return run_engine(list(records_), budget=4096, selected_domain=None)


def test_every_pick_goes_out_short_in_score_order():
    picks = [pick(index) for index in range(1, 6)]
    response = respond(picks)
    assert [line["record_id"] for line in lines(response)] == [item.record_id for item, _ in picks]
    assert response["selected_ids"] == [item.record_id for item, _ in picks]
    assert not any({"ontology", "evidence_ids", "case_ids", "source_receipt_ids"} & set(line) for line in lines(response))


def test_picks_that_would_all_fit_whole_still_go_out_short():
    assert not any("ontology" in line for line in lines(respond([pick()])))


def test_a_plain_pick_has_no_selector_block_at_all():
    assert not any("selector" in line for line in lines(respond([pick(1), pick(2)])))


@pytest.mark.parametrize("text", [RULE, f"{BACKGROUND}{RULE} {BACKGROUND}"])
def test_a_standing_rule_keeps_its_mark_and_nothing_else(text):
    item, score = pick(1, text=text)
    first, second, *_ = lines(respond([(item, score), pick(2), *CROWD], reserved={item.record_id: [RULE]}))
    assert first["selector"] == {"reserved": True}
    assert first["text"].startswith(RULE)
    assert "selector" not in second


def test_only_one_line_of_a_payload_says_how_to_look_the_rest_up():
    assert ["detail" in line for line in lines(respond([pick(1), pick(2), pick(3)]))] == [True, False, False]


def test_a_local_line_that_said_it_saves_the_picks_saying_it():
    response = respond([pick(1), pick(2)], local=local_result(record(50)))
    assert ["detail" in line for line in lines(response)] == [True, False, False]


def test_a_rule_whose_whole_text_went_out_is_latched_not_retried():
    picks = [pick(index) for index in range(1, 4)]
    response = respond(picks)
    assert response["retry_ids"] == []
    assert response["selected_ids"] == [item.record_id for item, _ in picks]


def test_a_rule_at_the_cut_limit_is_latched_too():
    assert respond([pick(1, text="x" * COMPACT_TEXT_BYTES)])["retry_ids"] == []


def test_a_rule_whose_text_was_cut_is_retried():
    item, score = pick(1, text=BACKGROUND * 2)
    response = respond([(item, score), *CROWD])
    assert response["retry_ids"] == [item.record_id]
    assert lines(response)[0]["text_complete"] is False


LONG_RULE = (
    "Never email the client directly. The billing contact is the one exception, and invoices go straight to them. "
    f"{BACKGROUND[:320]}Thanks for pulling this together."
)


def test_a_long_pick_goes_whole_when_the_budget_has_room_for_it():
    item, score = pick(1, text=LONG_RULE)
    response = respond([(item, score)])
    (line,) = lines(response)
    assert line == json.loads(_render(item, selector={"mode": "jev", "rank": 1, "score": score}))
    assert "The billing contact is the one exception" in line["text"]
    assert response["retry_ids"] == []
    assert "delivered_short" not in response["selector_stages"]


def test_a_long_pick_goes_whole_beside_the_short_lines_of_the_rest():
    item, score = pick(1, text=LONG_RULE)
    whole, *rest = lines(respond([(item, score), pick(2), pick(3)]))
    assert whole["text"] == item.text
    assert [("ontology" in line, "detail" in line) for line in rest] == [(False, True), (False, False)]


def test_the_picks_after_a_long_one_keep_their_room():
    item, score = pick(1, text=LONG_RULE)
    rest = [pick(2), pick(3)]
    whole = _render(item, selector={"mode": "jev", "rank": 1, "score": score})
    beside = len(render_short(rest[0][0], lookup=True)) + len(render_short(rest[1][0]))
    fits = respond([(item, score), *rest], budget=len(whole) + beside)
    assert lines(fits)[0]["text"] == item.text
    assert fits["selector_stages"]["delivered"] == 3
    short = respond([(item, score), *rest], budget=len(whole) + beside - 1)
    assert lines(short)[0]["text_complete"] is False
    assert short["selector_stages"]["delivered"] == 3
    assert short["retry_ids"] == [item.record_id]


def test_a_long_pick_gives_up_its_whole_line_before_a_later_pick_loses_its_own():
    item, score = pick(1, text=LONG_RULE)
    response = respond([(item, score), *CROWD])
    assert lines(response)[0]["text_complete"] is False
    room = 4096 - len(render_short(item, lookup=True))
    assert response["selector_stages"]["delivered"] == 1 + room // len(render_short(CROWD[0][0]))


def test_a_long_standing_rule_with_room_goes_whole_with_its_mark():
    item, score = pick(1, text=LONG_RULE)
    (line,) = lines(respond([(item, score)], reserved={item.record_id: ["Never email the client directly."]}))
    assert line["text"] == item.text
    assert line["selector"] == {"mode": "jev", "rank": 1, "score": score, "reserved": True}


def test_a_pick_short_enough_for_a_short_line_stays_on_one_with_room_to_spare():
    assert not any("ontology" in line for line in lines(respond([pick(1, text="x" * COMPACT_TEXT_BYTES)])))


def test_a_long_pick_is_cut_once_a_turn(monkeypatch):
    reads = []
    cut = engine._compact_text
    monkeypatch.setattr(engine, "_compact_text", lambda text, rules=(), **how: (reads.append(text) or cut(text, rules, **how)))
    picks = [pick(index, text=f"Rule {index}. {BACKGROUND * 2}") for index in range(1, 11)]
    respond(picks)
    assert len(reads) <= len(picks) + 1


def test_a_cut_rule_that_can_never_fit_whole_is_latched_from_its_short_line():
    assert respond([pick(1, text=BACKGROUND * 60)])["retry_ids"] == []


def test_a_cut_rule_owed_a_whole_delivery_gets_it_when_there_is_room():
    item, score = pick(1, text=BACKGROUND * 2)
    response = respond([(item, score), pick(2)], retried=frozenset({item.record_id}))
    whole, short = lines(response)
    assert whole["text"] == item.text
    assert whole["selector"] == {"mode": "jev", "rank": 1, "score": score}
    assert whole["ontology"]["type"] == "Verdict"
    assert (short["detail"], "ontology" in short) == (SHORT_LINE_DETAIL, False)
    assert response["retry_ids"] == []


def test_a_cut_rule_stays_owed_while_there_is_no_room_for_it():
    first = pick(1, text="x" * COMPACT_TEXT_BYTES)
    owed, score = pick(2, text="x" * 800)
    whole = _render(owed, selector={"mode": "jev", "rank": 2, "score": score})
    response = respond([first, (owed, score)], budget=len(whole), retried=frozenset({owed.record_id}))
    line = lines(response)[-1]
    assert (line["record_id"], line["text_complete"]) == (owed.record_id, False)
    assert response["retry_ids"] == [owed.record_id]


def test_a_pick_whose_text_fits_is_not_owed_a_whole_line_it_was_owed_before():
    item, score = pick(1)
    response = respond([(item, score)], retried=frozenset({item.record_id}))
    assert not any("ontology" in line for line in lines(response))
    assert response["retry_ids"] == []


def test_a_cut_local_record_is_not_listed_as_owed():
    long = record(50, text=BACKGROUND * 2)
    response = respond([pick(1)], local=local_result(long))
    assert long.record_id in response["selected_ids"]
    assert response["retry_ids"] == []


def test_short_picks_are_counted_apart_from_compact_ones():
    assert respond([pick(1), pick(2)])["selector_stages"] == {
        "eligible": 2, "picked": 2, "delivered": 2, "delivered_compact": 0, "unfit": 0,
        "local_records": 0, "local_bytes": 0, "delivered_short": 2,
    }


@pytest.mark.parametrize("budget", [4096, 4097, 4099, 8192])
@pytest.mark.parametrize("count", [1, 20, 150])
def test_picks_on_short_lines_never_exceed_the_budget(budget, count):
    texts = [RULE, "x" * COMPACT_TEXT_BYTES, BACKGROUND * 3]
    picks = [pick(index, text=texts[index % 3]) for index in range(count)]
    retried = frozenset(item.record_id for item, _ in picks[::2])
    assert len(respond(picks, budget=budget, retried=retried)["payload"].encode("utf-8")) <= budget


def test_a_pick_with_many_references_no_longer_runs_out_of_room():
    item, score = pick(1, evidence_ids=tuple(f"urn:synthetic:evidence:{index}" for index in range(200)))
    assert respond([(item, score)])["selector_outcome"] == "picked"


def test_a_pick_the_local_records_left_no_room_for_is_still_called_crowded():
    local = local_result(record(50))
    assert respond([pick(1)], budget=len(local.payload) + 20, local=local)["selector_outcome"] == "crowded_picks"


def selector_config(mode: str = "jev", **overrides) -> dict:
    value = json.loads(json.dumps(DEFAULTS["selector"]))
    value["mode"] = mode
    value["jev"]["api_key_env"] = KEY_ENV
    value["jev"].update(overrides)
    return value


class Transport:
    def __init__(self):
        self.statements: list[set[str]] = []
        self.error: Exception | None = None

    def __call__(self, endpoint, body, headers, timeout):
        questions = json.loads(body)["questions"]
        self.statements.append({
            question["instructions"][len(QUESTION_PREFIX):] for question in questions.values()
        })
        if self.error is not None:
            raise self.error
        return json.dumps({"answers": {
            name: {"type": "noul", "noul": 0.9} for name in questions
        }}).encode("utf-8")


@pytest.fixture
def transport(monkeypatch) -> Transport:
    fake = Transport()
    monkeypatch.setattr(jev, "http_post", fake)
    return fake


def select(root, state, *, store=None, **overrides):
    return select_prompt_payload(
        store or FakeStore(), root=root, session_id="synthetic-session", state=state, explicit_domain=None,
        budget=4096, selector=selector_config(**overrides), mode="jev", short_lines=True,
    )


def commit(root, decision) -> None:
    assert retrieval.commit_payload_delivery(
        root=root, session_id="synthetic-session", snapshot_id=str(decision.response["snapshot_id"]),
        domain_id=str(decision.response["receipt_scope"]),
    )


def test_a_rule_delivered_whole_on_a_short_line_is_not_sent_again(tmp_path, transport, records):
    records.extend(record(index, text=f"Never use template {index}.") for index in range(3))
    first = select(tmp_path, "first turn")
    assert len(first.response["selected_ids"]) == 3
    commit(tmp_path, first)
    second = select(tmp_path, "second turn")
    assert second.outcome == "no_picks"
    assert second.response["selected_ids"] == []
    assert len(transport.statements) == 1


def tight_budget(long: RetrievalRecord) -> int:
    """Room for a long pick's whole line alone, so a plain pick beside it has it cut."""
    return len(_render(long, selector={"mode": "jev", "rank": 1, "score": 0.9})) + 40


def test_a_rule_cut_on_a_short_line_arrives_whole_once_on_a_later_turn(tmp_path, transport, records):
    long = record(1, text=f"Never use the vendor template. {BACKGROUND * 2}")
    records.extend([long, record(2, text="Never ship on a Friday.")])
    budget = tight_budget(long)
    first = select(tmp_path, "first turn", budget_bytes=budget)
    assert first.response["retry_ids"] == [long.record_id]
    commit(tmp_path, first)
    second = select(tmp_path, "second turn", budget_bytes=budget)
    assert len(transport.statements[1]) == 1
    (line,) = [json.loads(item) for item in second.response["payload"].splitlines()]
    assert (line["record_id"], line["text"]) == (long.record_id, long.text)
    assert second.response["retry_ids"] == []
    commit(tmp_path, second)
    assert select(tmp_path, "third turn", budget_bytes=budget).outcome == "no_picks"
    assert len(transport.statements) == 2


def test_a_fallback_keeps_out_what_went_whole_and_what_it_could_only_repeat_cut(tmp_path, transport, records):
    long = record(1, text=f"Never use the vendor template. {BACKGROUND * 2}")
    plain = record(2, text="Never ship on a Friday.")
    records.extend([long, plain])
    budget = tight_budget(long)
    first = select(tmp_path, "first turn", budget_bytes=budget)
    assert first.response["retry_ids"] == [long.record_id]
    commit(tmp_path, first)
    transport.error = TimeoutError("synthetic timeout")
    fallback = select(tmp_path, "second turn", budget_bytes=budget)
    assert fallback.fallback_reason == "timeout"
    assert fallback.exclude_ids == frozenset({long.record_id, plain.record_id})


def test_on_whole_lines_a_fallback_can_still_deliver_a_compact_pick_whole(tmp_path, transport, records):
    records.extend(record(index, text=f"Never use template {index}. {BACKGROUND * 4}") for index in range(1, 3))

    def whole_route(state):
        return select_prompt_payload(
            FakeStore(), root=tmp_path, session_id="synthetic-session", state=state, explicit_domain=None,
            budget=4096, selector=selector_config(), mode="jev",
        )
    first = whole_route("first turn")
    assert first.response["retry_ids"] == [item.record_id for item in records]
    commit(tmp_path, first)
    transport.error = TimeoutError("synthetic timeout")
    assert whole_route("second turn").exclude_ids == frozenset()


@pytest.fixture
def snapshots(monkeypatch):
    """Let a test move the store on between turns."""
    moved = {"turn": 0}
    monkeypatch.setattr(retrieval, "_snapshot_id", lambda store: f"synthetic-snapshot-{moved['turn']}")
    return moved


def test_an_outage_sends_a_cut_record_once_however_often_the_store_moves(
    tmp_path, records, snapshots, transport, monkeypatch, capsys,
):
    long = [
        record(index, text=f"Long rule {index}. {BACKGROUND}", section="domain", domain_id="research")
        for index in range(1, 5)
    ]
    plain = [
        record(index, text=f"Plain rule {index}.", section="domain", domain_id="research")
        for index in range(5, 40)
    ]
    records.extend(long + plain)
    transport.error = TimeoutError("synthetic timeout")
    config = jev_config(tmp_path, **{SHORT_LINES_PROMPT: True})
    sent: Counter[str] = Counter()
    for turn in range(10):
        snapshots["turn"] = turn
        event = {"session_id": "s", "prompt": f"turn {turn}", "domain_id": "research"}
        body = hook(config, "user-prompt-submit", event, monkeypatch, capsys)
        assert body["selector_method"] == "deterministic_fallback"
        sent.update(line["record_id"] for line in delivered(body))
    assert sent == Counter({item.record_id: 1 for item in records})


def test_a_cut_record_an_outage_sent_is_settled_by_what_happens_to_it_after(
    tmp_path, records, snapshots, transport, monkeypatch, capsys,
):
    pick_ = record(1, text=f"Never use the vendor template. {BACKGROUND * 2}", section="domain", domain_id="research")
    offer = record(2, text=f"An offer with a long description. {BACKGROUND * 2}", section="domain", domain_id="research",
                   ontology_partition="business_declared", ontology_type="Offer", judgment_statement=None)
    records.extend([pick_, offer])
    config = jev_config(tmp_path, **{SHORT_LINES_PROMPT: True})
    transport.error = TimeoutError("synthetic timeout")
    event = {"session_id": "s", "domain_id": "research"}
    outage = hook(config, "user-prompt-submit", {**event, "prompt": "outage"}, monkeypatch, capsys)
    assert {line["record_id"]: line["text_complete"] for line in delivered(outage)} == {
        pick_.record_id: False, offer.record_id: False,
    }
    transport.error = None
    snapshots["turn"] = 1
    recovered = hook(config, "user-prompt-submit", {**event, "prompt": "recovered"}, monkeypatch, capsys)
    assert recovered["selector_method"] == "jev"
    (line,) = delivered(recovered)
    assert (line["record_id"], line["text"]) == (pick_.record_id, pick_.text)


def test_a_cut_local_record_is_not_sent_again_when_jev_comes_back(tmp_path, records, snapshots, transport, monkeypatch, capsys):
    offer = record(2, text=f"An offer with a long description. {BACKGROUND * 2}", section="domain", domain_id="research",
                   ontology_partition="business_declared", ontology_type="Offer", judgment_statement=None)
    records.extend([offer, record(3, text="Never ship on a Friday.")])
    config = jev_config(tmp_path, **{SHORT_LINES_PROMPT: True})
    transport.error = TimeoutError("synthetic timeout")
    event = {"session_id": "s", "domain_id": "research"}
    hook(config, "user-prompt-submit", {**event, "prompt": "outage"}, monkeypatch, capsys)
    transport.error = None
    snapshots["turn"] = 1
    recovered = hook(config, "user-prompt-submit", {**event, "prompt": "recovered"}, monkeypatch, capsys)
    assert offer.record_id not in [line["record_id"] for line in delivered(recovered)]


def test_a_cut_local_record_is_sent_once_not_on_every_turn(tmp_path, transport, records):
    local = record(9, text=f"An offer with a long description. {BACKGROUND * 2}", section="domain", domain_id="research",
                   ontology_partition="business_declared", ontology_type="Offer", judgment_statement=None)
    records.extend([local, record(1, text="Never ship on a Friday."), record(2, text="Never skip review.")])
    sent = 0
    for turn in range(4):
        decision = select_prompt_payload(
            FakeStore(), root=tmp_path, session_id="synthetic-session", state=f"turn {turn}", explicit_domain="research",
            budget=4096, selector=selector_config(), mode="jev", short_lines=True,
        )
        sent += decision.response["selected_ids"].count(local.record_id)
        if decision.response["status"] == "delivered":
            commit(tmp_path, decision)
    assert sent == 1


def test_a_standing_rule_reaches_its_short_line_with_its_mark(tmp_path, transport, records):
    statement = "From now on, a release note should be plain text."
    records.append(record(1, text=statement, valid_from="2026-01-01T00:00:00Z"))
    records.extend(record(index, text=f"No, the {index} banner stays.") for index in range(2, 5))
    decision = select(
        tmp_path, "write the release note", score_floor=0.95,
        standing_rules={"enabled": True, "score_floor": 0.25, "rare_word_share": 0.5, "slots": 2, "keep": 30},
    )
    (line,) = [json.loads(item) for item in decision.response["payload"].splitlines()]
    assert (line["text"], line["selector"]) == (statement, {"reserved": True})
    assert decision.response["selector_stages"]["reserved"] == 1


# -- the two routes ------------------------------------------------------------------

RESEARCH = [{"domain_id": "research", "public_label": "Research"}]


@pytest.fixture
def routes(tmp_path, records):
    """Both routes over the same records: session start and a matched domain."""
    records.append(record(1, text=RULE))
    records.append(record(2, text="Report the failed source.", section="domain", domain_id="research"))

    def run(monkeypatch, capsys, session="s", **config_values):
        config, _ = write_config(tmp_path, domains=RESEARCH, **config_values)
        start = hook(config, "session-start", {"session_id": session}, monkeypatch, capsys)
        prompt = hook(
            config, "user-prompt-submit", {"session_id": session, "prompt": "go", "domain_id": "research"},
            monkeypatch, capsys,
        )
        return delivered(start), delivered(prompt)
    return run


def is_short(line: dict) -> bool:
    return "ontology" not in line and "evidence_ids" not in line


def test_with_both_switches_off_both_routes_stay_whole(routes, monkeypatch, capsys):
    start, prompt = routes(monkeypatch, capsys)
    assert not any(map(is_short, start + prompt))


def test_the_prompt_switch_leaves_session_start_whole(routes, monkeypatch, capsys):
    start, prompt = routes(monkeypatch, capsys, **{SHORT_LINES_PROMPT: True})
    assert not any(map(is_short, start))
    assert all(map(is_short, prompt))


def test_the_session_start_switch_leaves_the_prompt_route_whole(routes, monkeypatch, capsys):
    start, prompt = routes(monkeypatch, capsys, **{SHORT_LINES_SESSION_START: True})
    assert all(map(is_short, start))
    assert not any(map(is_short, prompt))


def test_session_start_redelivers_the_same_bytes_after_a_compact(tmp_path, records, monkeypatch, capsys):
    records.extend(record(index, text=f"Rule {index}. {BACKGROUND if index % 2 else ''}") for index in range(8))
    config, _ = write_config(tmp_path, **{SHORT_LINES_SESSION_START: True})
    startup = hook(config, "session-start", {"session_id": "s", "source": "startup"}, monkeypatch, capsys)
    compact = hook(config, "session-start", {"session_id": "s", "source": "compact"}, monkeypatch, capsys)
    other = hook(config, "session-start", {"session_id": "other", "source": "startup"}, monkeypatch, capsys)
    assert startup["hookSpecificOutput"] == compact["hookSpecificOutput"] == other["hookSpecificOutput"]
    assert startup["hookSpecificOutput"]["additionalContext"]


def jev_config(tmp_path, mode="jev", *, jev_values=None, **values):
    return write_config(tmp_path, selector=selector_config(mode, **(jev_values or {})), domains=RESEARCH, **values)[0]


def test_a_record_cut_at_session_start_is_judged_by_the_budget_that_picks_it(tmp_path, records, transport, monkeypatch, capsys):
    long = record(1, text=f"Never use the vendor template. {BACKGROUND * 6}")
    records.append(long)
    assert 4096 < len(_render(long)) < 16384
    config = jev_config(tmp_path, jev_values={"budget_bytes": 16384}, **{SHORT_LINES_SESSION_START: True, SHORT_LINES_PROMPT: True})
    (cut,) = delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))
    assert cut["text_complete"] is False
    body = hook(config, "user-prompt-submit", {"session_id": "s", "prompt": "go"}, monkeypatch, capsys)
    assert [line["text"] for line in delivered(body)] == [long.text]


def test_a_record_that_can_never_fit_whole_is_settled_by_the_route_that_picks_it(tmp_path, records, transport, monkeypatch, capsys):
    long = record(1, text=f"Never use the vendor template. {BACKGROUND * 60}")
    records.append(long)
    config = jev_config(tmp_path, **{SHORT_LINES_SESSION_START: True, SHORT_LINES_PROMPT: True})
    hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys)
    picked = hook(config, "user-prompt-submit", {"session_id": "s", "prompt": "go"}, monkeypatch, capsys)
    assert [line["text_complete"] for line in delivered(picked)] == [False]
    again = hook(config, "user-prompt-submit", {"session_id": "s", "prompt": "go on"}, monkeypatch, capsys)
    assert delivered(again) == [] and again["selector_outcome"] == "no_picks"


def test_the_prompt_switch_covers_picks_and_their_local_records(tmp_path, records, transport, monkeypatch, capsys):
    records.append(record(1, text=RULE))
    records.append(record(2, text="A declared offer.", section="domain", domain_id="research",
                          ontology_partition="business_declared", ontology_type="Offer", judgment_statement=None))
    config = jev_config(tmp_path, **{SHORT_LINES_PROMPT: True})
    event = {"session_id": "s", "prompt": "go", "domain_id": "research"}
    body = hook(config, "user-prompt-submit", event, monkeypatch, capsys)
    assert body["selector_method"] == "jev"
    assert [line["record_id"] for line in delivered(body)] == [records[1].record_id, records[0].record_id]
    assert all(map(is_short, delivered(body)))


def test_the_prompt_switch_covers_a_fallback(tmp_path, records, transport, monkeypatch, capsys):
    records.append(record(1, text="Report the failed source.", section="domain", domain_id="research"))
    transport.error = TimeoutError("synthetic timeout")
    config = jev_config(tmp_path, **{SHORT_LINES_PROMPT: True})
    event = {"session_id": "s", "prompt": "go", "domain_id": "research"}
    body = hook(config, "user-prompt-submit", event, monkeypatch, capsys)
    assert body["selector_method"] == "deterministic_fallback"
    assert all(map(is_short, delivered(body))) and delivered(body)


def test_shadow_still_delivers_what_deterministic_delivers(tmp_path, records, transport, monkeypatch, capsys):
    records.append(record(1, text="Report the failed source.", section="domain", domain_id="research"))
    event = {"prompt": "go", "domain_id": "research"}
    bodies = []
    for name, mode in (("shadow", "shadow"), ("deterministic", "deterministic")):
        (tmp_path / name).mkdir()
        config = jev_config(tmp_path / name, mode, **{SHORT_LINES_PROMPT: True})
        bodies.append(hook(config, "user-prompt-submit", {**event, "session_id": name}, monkeypatch, capsys))
    assert bodies[0]["hookSpecificOutput"] == bodies[1]["hookSpecificOutput"]
    assert all(map(is_short, delivered(bodies[0])))


def cli_retrieve(config: Path, capsys, *arguments) -> dict:
    assert cli.main(["--config", str(config), "retrieve", "--session", "by-hand", *arguments]) == 0
    return json.loads(capsys.readouterr().out)


def test_explicit_retrieval_keeps_whole_lines_with_both_switches_on(tmp_path, records, capsys):
    records.append(record(1, text=RULE))
    config, _ = write_config(tmp_path, **{SHORT_LINES_PROMPT: True, SHORT_LINES_SESSION_START: True})
    assert not any(map(is_short, map(json.loads, cli_retrieve(config, capsys, "--refresh")["payload"].splitlines())))


def test_outside_jev_a_short_line_is_latched_per_snapshot_as_a_whole_line_is(
    tmp_path, records, snapshots, monkeypatch, capsys,
):
    records.append(record(1, text=RULE, section="domain", domain_id="research"))
    config = write_config(tmp_path, domains=RESEARCH, **{SHORT_LINES_PROMPT: True})[0]
    event = {"session_id": "s", "prompt": "go", "domain_id": "research"}
    assert len(delivered(hook(config, "user-prompt-submit", event, monkeypatch, capsys))) == 1
    assert hook(config, "user-prompt-submit", event, monkeypatch, capsys)["status"] == "already_delivered"
    snapshots["turn"] = 1
    assert len(delivered(hook(config, "user-prompt-submit", event, monkeypatch, capsys))) == 1


def test_session_start_does_not_read_stale_before(tmp_path, records, monkeypatch, capsys):
    words = "Sounds fine, thanks for the update."
    records.append(record(1, text=words, capture_mechanism="claude_code_stop_hook", valid_from="2026-01-01T00:00:00Z"))
    assert not jev.StaleCheck.since("2026-06-01T00:00:00Z").keeps(records[0])
    config = write_config(
        tmp_path, selector=selector_config("deterministic", stale_before="2026-06-01T00:00:00Z"),
        **{SHORT_LINES_SESSION_START: True},
    )[0]
    assert [line["text"] for line in delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))] == [words]


def test_explicit_retrieval_of_a_prompt_keeps_whole_lines_under_jev(tmp_path, records, transport, capsys):
    records.append(record(1, text=RULE))
    config = jev_config(tmp_path, **{SHORT_LINES_PROMPT: True, SHORT_LINES_SESSION_START: True})
    response = cli_retrieve(config, capsys, "--refresh", "--prompt", "go")
    assert response["selector_method"] == "jev"
    (line,) = [json.loads(item) for item in response["payload"].splitlines()]
    assert not is_short(line)
    assert line["selector"]["mode"] == "jev"


# -- capacity ------------------------------------------------------------------------
#
# Measured on the installed budget of 4,096 bytes, with a record of realistic size:
# a full urn:imprint:verdict:<uuid> id and 160 bytes of text. A short line holding
# them is 266 bytes. Twelve at session start needs 12 lines and the lookup in 3,072
# bytes, or 252 bytes a line, and the three required keys and an id cost 106 of that.
#
# Plain text of exactly 160 bytes is the best case. Fifteen beside the default two
# reserved rules leaves 9 bytes of the whole payload, so the tests below it say what
# escaped text, longer text and imported rules cost.

CAPTURED = ("captured", "captured_judgment")
RATIFIED = ("ratified", "ratified_knowledge")
IMPORTED = ("extracted", "imported_floor")
RULE_BYTES = 160


def urn(kind: str, index: int) -> str:
    return f"urn:imprint:{kind}:{uuid.uuid5(uuid.NAMESPACE_URL, f'synthetic-{kind}-{index}')}"


def rule_text(index: int, size: int = RULE_BYTES) -> str:
    return (f"Rule {index:03d}: keep the failed source in the summary and say plainly what was not checked. " * 3)[:size]


def prose_text(index: int, size: int = RULE_BYTES) -> str:
    """Rule text as people write it: JSON escapes its quotes and its line break."""
    return (f'Rule {index:03d}: don\'t say "done" until the "failed" source is named.\n'
            'Then say plainly what was not checked, and keep the summary to the point of it. ' * 3)[:size]


def realistic(count: int, *, section: str = "general", authority=CAPTURED, size: int = RULE_BYTES, start: int = 0,
              words=rule_text):
    return [
        RetrievalRecord(
            record_id=urn("verdict", index), text=words(index, size), section=section,
            provenance_status=authority[0], authority_tier=authority[1],
            evidence_ids=(urn("evidence", index),), case_ids=(urn("case", index),),
            case_summaries=("Explicit operator feedback witnessed by explicit hook input",),
            source_receipt_ids=(urn("evidence", index),), provenance_complete=True,
            imported_selected=authority == IMPORTED, ontology_type="Verdict",
            ontology_path=("judgment", "Verdict"), judgment_statement=words(index, size),
        )
        for index in range(start, start + count)
    ]


def picked(items) -> list[tuple[RetrievalRecord, float]]:
    return [(item, 0.9) for item in items]


def session_start(root, **options) -> dict:
    return retrieve_payload(FakeStore(), root=root, session_id="s", budget=4096, refresh=True, **options)


def test_a_realistic_rule_costs_266_bytes_on_a_short_line():
    (item,) = realistic(1)
    assert len(item.text.encode("utf-8")) == RULE_BYTES
    assert len(render_short(item)) == 266


def test_fifteen_realistic_rules_fit_a_prompt():
    response = respond(picked(realistic(20)))
    assert response["selector_stages"]["delivered"] == 15
    assert response["selector_stages"]["unfit"] == 5
    assert response["selected_bytes"] <= 4096


MIXED = (CAPTURED, RATIFIED, IMPORTED)


def test_fifteen_fit_with_mixed_authorities():
    items = [realistic(1, authority=MIXED[index % len(MIXED)], start=index)[0] for index in range(20)]
    assert respond(picked(items))["selector_stages"]["delivered"] == 15


@pytest.mark.parametrize("authority", MIXED)
def test_every_authority_measured_here_is_one_retrieval_can_deliver(authority):
    assert engine.eligible(realistic(1, authority=authority)[0], None)


def test_the_fifteen_beside_two_reserved_rules_have_nine_bytes_to_spare():
    items = realistic(20)
    reserved = {item.record_id: ["Rule."] for item in items[:2]}
    assert respond(picked(items), reserved=reserved)["selected_bytes"] == 4096 - 9


@pytest.mark.parametrize("label, options, on_a_prompt, at_session_start", [
    ("text JSON has to escape", {"words": prose_text}, 14, 11),
    ("170 bytes", {"size": 170}, 14, 10),
    ("200 bytes", {"size": 200}, 13, 9),
])
def test_what_real_text_costs(tmp_path, records, label, options, on_a_prompt, at_session_start):
    assert respond(picked(realistic(20, **options)))["selector_stages"]["delivered"] == on_a_prompt, label
    records.extend(realistic(60, **options))
    assert len(session_start(tmp_path, short_lines=True)["selected_ids"]) == at_session_start, label


@pytest.mark.parametrize("marked, delivered_rules", [(2, 15), (5, 14)])
def test_the_reserved_mark_costs_one_rule_only_past_the_default_two_slots(marked, delivered_rules):
    items = realistic(20)
    reserved = {item.record_id: ["Rule."] for item in items[:marked]}
    assert respond(picked(items), reserved=reserved)["selector_stages"]["delivered"] == delivered_rules


def test_a_prompt_of_nothing_but_imported_rules_holds_one_fewer():
    assert respond(picked(realistic(20, authority=IMPORTED)))["selector_stages"]["delivered"] == 14


def test_eleven_realistic_rules_fit_session_start_not_twelve(tmp_path, records):
    records.extend(realistic(60))
    response = session_start(tmp_path, short_lines=True)
    assert len(response["selected_ids"]) == 11
    assert response["selected_bytes"] <= 3072
    assert response["omitted_count"] == 49


def test_eleven_fit_when_core_and_general_share_session_start(tmp_path, records):
    records.extend(realistic(30, section="core") + realistic(30, start=30))
    response = session_start(tmp_path, short_lines=True)
    assert len(response["selected_ids"]) == 11
    assert response["section_bytes"]["core"] <= 1024


@pytest.mark.parametrize("size, count", [(146, 12), (147, 11)])
def test_twelve_at_session_start_takes_text_of_146_bytes_or_less(tmp_path, records, size, count):
    records.extend(realistic(60, size=size))
    assert len(session_start(tmp_path, short_lines=True)["selected_ids"]) == count


def test_the_same_rules_on_whole_lines_fit_a_third_as_many(tmp_path, records):
    assert _jev_response(
        "snapshot", "scope", picked(realistic(20)), "picked", 20, 4096,
    )["selector_stages"]["delivered"] == 5
    records.extend(realistic(60))
    assert len(session_start(tmp_path)["selected_ids"]) == 4


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


def unreadable(store: ImprintStore) -> str:
    """A stored record retrieval cannot project, which the store accepts."""
    evidence = store.current_nodes(["Evidence"])
    return store.append_derived_node(
        node_type="Principle", payload={"statement": "Report failures explicitly.", "recurrence_count": None},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[item["node_id"] for item in evidence], operator_id=evidence[0]["operator_id"],
        valid_from="2026-07-14T18:00:00Z", proposed_by="contract-test",
    )


def test_one_unreadable_record_does_not_break_the_lookup_of_another(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    store = ImprintStore(root / "imprint.db")
    unreadable(store)
    (verdict,) = store.current_nodes(["Verdict"])
    found = history(config, verdict["node_id"], capsys)
    assert found["versions"] and found["whole_line"]["record_id"] == verdict["node_id"]


def test_a_record_retrieval_cannot_read_still_shows_its_history(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    node_id = unreadable(ImprintStore(root / "imprint.db"))
    found = history(config, node_id, capsys)
    assert found["versions"][-1]["payload"]["statement"] == "Report failures explicitly."
    assert found["whole_line"] is None


def test_an_unknown_record_still_fails(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    assert cli.main(["--config", str(config), "history", "urn:imprint:verdict:unknown"]) != 0
    capsys.readouterr()


# -- imprint details -----------------------------------------------------------------

CASE = "Synthetic review of a quarterly draft."
CONTEXT = "Synthetic thread text the operator was answering."
WORDS = "No, keep the failed source in the table."
UNKNOWN_RECORD = json.dumps({"error": "unknown record", "error_type": "ValidationError", "status": "error"}) + "\n"


def details(config: Path, record_id: str, capsys) -> tuple[int, str]:
    code = cli.main(["--config", str(config), "details", record_id])
    return code, capsys.readouterr().out


def run(config: Path, capsys, *words: str) -> None:
    assert cli.main(["--config", str(config), *words]) == 0
    capsys.readouterr()


def capture(root: Path, words: str = WORDS) -> dict:
    """Apply one capture carrying context evidence and an extension, and return its event."""
    store = ImprintStore(root / "imprint.db")
    envelope = build_capture_envelope(
        operator_id=store.current_nodes(["Evidence"])[0]["operator_id"], session_id=new_urn("session"),
        node_id="primary", case_description=CASE, raw_operator_text=words, call_type="prefer",
        capture_mechanism="explicit_cli", captured_by="synthetic-capture",
        contextual_evidence=[{"kind": "context", "content": CONTEXT, "source_locator": "synthetic:thread"}],
        extensions={"org.example.scope": {"schema_version": "1.0.0", "payload": {"client": "example-client"}}},
    )
    store.apply_capture(envelope)
    with store.connect() as conn:
        (payload,) = conn.execute(
            "SELECT payload_json FROM events WHERE event_id=?", (envelope["input_event_id"],),
        ).fetchone()
    return json.loads(payload)


def strings(value: object) -> set[str]:
    """Every string a JSON value holds, keys aside."""
    if isinstance(value, str):
        return {value}
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return {text for item in items for text in strings(item)}


def test_details_prints_a_records_whole_line_and_nothing_else(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    (line,) = delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))
    code, out = details(config, line["record_id"], capsys)
    assert code == 0
    assert len(out.splitlines()) == 1
    assert json.loads(out) == whole_line(ImprintStore(root / "imprint.db"), line["record_id"]) == line


def test_details_leaves_out_the_capture_behind_the_line(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    event = capture(root)
    verdict = event["verdict"]["verdict_id"]
    assert CONTEXT in strings(event) and event["session_id"] in strings(event)
    code, out = details(config, verdict, capsys)
    assert code == 0
    line = json.loads(out)
    assert set(line) == {
        "authority", "case_ids", "case_summaries", "evidence_ids", "ontology", "record_id",
        "source_receipt_ids", "text",
    }
    for kept_out in (
        CONTEXT, event["session_id"], "synthetic:thread", "synthetic-capture", "example-client",
        "org.example.scope", event["operator_id"], event["input_event_id"], event["captured_at"],
        event["verdict"]["call"]["call_id"], "event_payload", "versions", "dispositions",
    ):
        assert kept_out not in out
    assert strings(line) & strings(event) == {
        verdict, WORDS, CASE, event["case"]["case_id"], "captured", *event["provenance"]["evidence_ids"],
    }


def test_details_says_the_case_text_only_in_case_summaries(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    _, out = details(config, capture(root)["verdict"]["verdict_id"], capsys)
    line = json.loads(out)
    assert line.pop("case_summaries") == [CASE]
    assert out.count(CASE) == 1
    assert CASE not in json.dumps(line, ensure_ascii=False)


def test_every_id_without_a_line_answers_the_same(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    store = ImprintStore(root / "imprint.db")
    replacement, tombstoned, superseded = (capture(root, f"{WORDS} {n}")["verdict"]["verdict_id"] for n in range(3))
    rejected = proposal(store)
    bad = unreadable(store)
    for node_id in (tombstoned, superseded, rejected):
        assert details(config, node_id, capsys)[0] == 0
    evidence = store.current_nodes(["Evidence"])[0]["node_id"]
    run(config, capsys, "delete", "tombstone", "--scope", tombstoned, "--reason", "synthetic")
    run(config, capsys, "review", "reject", rejected, "--by", "operator", "--reason", "too broad")
    run(config, capsys, "transition", "supersede", replacement, superseded,
        "--reason", "synthetic", "--evidence", evidence, "--by", "operator")
    ids = {
        "evidence": evidence,
        "case": store.current_nodes(["Case"])[0]["node_id"],
        "call": store.current_nodes(["Call"])[0]["node_id"],
        "unknown": "urn:imprint:verdict:unknown",
        "tombstoned": tombstoned,
        "rejected": rejected,
        "superseded": superseded,
        "unreadable": bad,
    }
    assert {kind: details(config, node_id, capsys) for kind, node_id in ids.items()} == dict.fromkeys(
        ids, (2, UNKNOWN_RECORD),
    )
    assert details(config, replacement, capsys)[0] == 0


@pytest.mark.parametrize("deferred", [False, True])
def test_a_proposal_answers_with_authority_inferred(deferred, tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    node_id = proposal(ImprintStore(root / "imprint.db"))
    if deferred:
        run(config, capsys, "review", "defer", node_id, "--by", "operator", "--reason", "not yet")
    code, out = details(config, node_id, capsys)
    assert (code, json.loads(out)["authority"]) == (0, "inferred")


def test_the_detail_on_a_short_line_runs_as_it_is_written(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path, **{SHORT_LINES_SESSION_START: True})
    words = f"{BACKGROUND}No, report the failed source because omissions change it."
    seed(config, monkeypatch, capsys, words)
    (line,) = delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))
    assert line["text_complete"] is False
    program, *arguments = shlex.split(line["detail"].replace("<record_id>", line["record_id"]))
    assert program == "imprint"
    done = subprocess.run(
        [sys.executable, "-m", "imprint.cli", *arguments], text=True, capture_output=True, check=False,
        env={**os.environ, "IMPRINT_CONFIG": str(config)},
    )
    assert (done.returncode, done.stderr) == (0, "")
    found = json.loads(done.stdout)
    assert found == whole_line(ImprintStore(root / "imprint.db"), line["record_id"])
    assert found["text"] == words


def test_a_compact_line_keeps_its_own_detail():
    item = record()
    assert json.loads(render_compact(item))["detail"] == f"imprint history {item.record_id}"


# -- what the docs promise -----------------------------------------------------------

REPOSITORY = Path(__file__).resolve().parents[2]


def page(name: str) -> str:
    return " ".join((REPOSITORY / name).read_text(encoding="utf-8").split())


def short_lines_section() -> str:
    text = page("docs/configuration.md")
    start = text.index("## Short lines")
    return text[start:text.index("## Environment overrides", start)]


def changelog_entry() -> str:
    text = page("CHANGELOG.md")
    start = text.index("Add short delivered lines")
    return text[start:text.index("Add `whole_line` to `imprint history`", start)]


def test_the_docs_limit_the_promise_not_to_resend_to_jev_mode():
    section = short_lines_section()
    assert "In `jev` mode, a rule whose whole text went out on a short line" in section
    assert "In `deterministic` and `shadow` mode, and on a T3 scratch turn" in section
    assert "for one store snapshot" in section
    assert "Under Jev, a rule whose whole text went out" in changelog_entry()


def test_the_docs_warn_that_session_start_ignores_stale_before():
    text = page("docs/configuration.md")
    start = text.index("`org.imprint.short_lines.session_start`: `false`")
    switch = text[start:text.index("The release artifact includes", start)]
    assert "does not read `selector.jev.stale_before`" in switch
    assert "`selector.jev.stale_before`" in changelog_entry()


def test_the_docs_call_the_best_case_the_best_case():
    section = short_lines_section()
    assert "that is the best case" in section
    assert "Expect about 14 on a prompt and 10 to 11 at session start" in section
    assert "about 14 and 10 to 11" in changelog_entry()
