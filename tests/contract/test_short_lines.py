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


# -- the selector route --------------------------------------------------------------

def pick(index: int = 1, **values) -> tuple[RetrievalRecord, float]:
    return record(index, **values), round(0.9 - index / 1000, 4)


def respond(picks, *, budget=4096, local=None, retried=frozenset(), reserved=None):
    return _jev_response(
        "snapshot", "scope", picks, "picked", len(picks), budget, local, retried, reserved, 0, True,
    )


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
    first, second = lines(respond([(item, score), pick(2)], reserved={item.record_id: [RULE]}))
    assert first["selector"] == {"reserved": True}
    assert first["text"] == (text if text == RULE else RULE)
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
    response = respond([(item, score)])
    assert response["retry_ids"] == [item.record_id]
    assert lines(response)[0]["text_complete"] is False


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


def test_cut_local_records_keep_their_claim_on_a_whole_delivery():
    long = record(50, text=BACKGROUND * 2)
    assert respond([pick(1)], local=local_result(long))["retry_ids"] == [long.record_id]


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


def test_a_rule_cut_on_a_short_line_arrives_whole_once_on_a_later_turn(tmp_path, transport, records):
    long = record(1, text=f"Never use the vendor template. {BACKGROUND * 2}")
    records.extend([long, record(2, text="Never ship on a Friday.")])
    first = select(tmp_path, "first turn")
    assert first.response["retry_ids"] == [long.record_id]
    commit(tmp_path, first)
    second = select(tmp_path, "second turn")
    assert len(transport.statements[1]) == 1
    (line,) = [json.loads(item) for item in second.response["payload"].splitlines()]
    assert (line["record_id"], line["text"]) == (long.record_id, long.text)
    assert second.response["retry_ids"] == []
    commit(tmp_path, second)
    assert select(tmp_path, "third turn").outcome == "no_picks"
    assert len(transport.statements) == 2


def test_the_fallback_keeps_out_what_went_whole_and_not_what_was_cut(tmp_path, transport, records):
    long = record(1, text=f"Never use the vendor template. {BACKGROUND * 2}")
    plain = record(2, text="Never ship on a Friday.")
    records.extend([long, plain])
    commit(tmp_path, select(tmp_path, "first turn"))
    transport.error = TimeoutError("synthetic timeout")
    fallback = select(tmp_path, "second turn")
    assert fallback.fallback_reason == "timeout"
    assert fallback.exclude_ids == frozenset({plain.record_id})


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


def jev_config(tmp_path, mode="jev", **values):
    return write_config(tmp_path, selector=selector_config(mode), domains=RESEARCH, **values)[0]


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

CAPTURED = ("captured", "captured_judgment")
RATIFIED = ("ratified", "ratified_knowledge")
EXTRACTED = ("extracted", "ratified_knowledge")
IMPORTED = ("extracted", "imported_floor")
RULE_BYTES = 160


def urn(kind: str, index: int) -> str:
    return f"urn:imprint:{kind}:{uuid.uuid5(uuid.NAMESPACE_URL, f'synthetic-{kind}-{index}')}"


def rule_text(index: int, size: int = RULE_BYTES) -> str:
    return (f"Rule {index:03d}: keep the failed source in the summary and say plainly what was not checked. " * 3)[:size]


def realistic(count: int, *, section: str = "general", authority=CAPTURED, size: int = RULE_BYTES, start: int = 0):
    return [
        RetrievalRecord(
            record_id=urn("verdict", index), text=rule_text(index, size), section=section,
            provenance_status=authority[0], authority_tier=authority[1],
            evidence_ids=(urn("evidence", index),), case_ids=(urn("case", index),),
            case_summaries=("Explicit operator feedback witnessed by explicit hook input",),
            source_receipt_ids=(urn("evidence", index),), provenance_complete=True,
            imported_selected=authority == IMPORTED, ontology_type="Verdict",
            ontology_path=("judgment", "Verdict"), judgment_statement=rule_text(index, size),
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


def test_fifteen_fit_with_mixed_authorities():
    kinds = (CAPTURED, RATIFIED, EXTRACTED, IMPORTED)
    items = [item for index, kind in enumerate(kinds) for item in realistic(5, authority=kind, start=index * 5)]
    assert respond(picked(items))["selector_stages"]["delivered"] == 15


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


def test_an_unknown_record_still_fails(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    assert cli.main(["--config", str(config), "history", "urn:imprint:verdict:unknown"]) != 0
    capsys.readouterr()
