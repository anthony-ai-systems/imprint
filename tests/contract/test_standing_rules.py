"""Standing rules get a reserved place, and stale records stop reaching Jev. Offline, synthetic."""

from __future__ import annotations

import copy
import json

import pytest

import imprint.retrieve as retrieval
from imprint.config import DEFAULTS, load_config
from imprint.errors import ValidationError
from imprint.retrieve import select_prompt_payload
from imprint.retrieve import jev
from imprint.retrieve.engine import render_compact
from imprint.retrieve.jev import (
    QUESTION_PREFIX, JevSelector, JevSettings, SelectionResult, decision_entry, prefilter, rare_matches,
    reads_as_feedback, rule_sentences, stale_filter,
)
from imprint.retrieve.models import RetrievalRecord

KEY_ENV = "JEV_TEST_KEY"
RULE = "From now on, a release note should be plain text, not HTML."
OFF_TOPIC_RULE = "Never rename the invoices folder."
CORRECTION = "No, that is wrong, the banner should stay blue."
STALE = "Please look into the invoice totals when you get a chance"
RULES_CHANGED = "2026-06-01T00:00:00Z"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)


def record(record_id: str, statement: str, **overrides) -> RetrievalRecord:
    values = dict(
        record_id=f"urn:synthetic:{record_id}", text=statement, section="general",
        provenance_status="captured", authority_tier="captured_judgment",
        evidence_ids=("urn:imprint:evidence:synthetic-e1",), case_ids=("urn:imprint:case:synthetic-c1",),
        case_summaries=("Synthetic case",), provenance_complete=True,
        ontology_type="Verdict", ontology_path=("judgment", "Verdict"), judgment_statement=statement,
        capture_mechanism="claude_code_stop_hook", valid_from="2026-01-01T00:00:00Z",
    )
    values.update(overrides)
    return RetrievalRecord(**values)


def settings(**overrides) -> JevSettings:
    values = dict(
        endpoint="https://example.invalid/", model="jev-test", api_key_env=KEY_ENV, score_floor=0.4,
        standing_floor=0.25, standing_rare_share=0.5, standing_slots=2,
    )
    values.update(overrides)
    return JevSettings(**values)


def scored(scores: dict[str, float], default: float = 0.1):
    def transport(endpoint, body, headers, timeout):
        questions = json.loads(body)["questions"]
        return json.dumps({"answers": {
            name: {"type": "noul", "noul": next(
                (value for text, value in scores.items() if question["instructions"] == QUESTION_PREFIX + text),
                default,
            )}
            for name, question in questions.items()
        }}).encode("utf-8")
    return transport


def selector_config(**overrides) -> dict:
    value = copy.deepcopy(DEFAULTS["selector"])
    value["mode"] = "jev"
    value["jev"]["api_key_env"] = KEY_ENV
    value["jev"].update(overrides)
    return value


class FakeStore:
    generation = 1

    def snapshot(self):
        return {"generation": self.generation}


@pytest.fixture
def records(monkeypatch):
    holder: list[RetrievalRecord] = []

    class FakeSource:
        def __init__(self, store):
            pass

        def retrieval_candidates(self, snapshot_id):
            return list(holder)

    monkeypatch.setattr(retrieval, "StoreRetrievalSource", FakeSource)
    return holder


def select(tmp_path, *, state="write the release note", budget=4096, **jev_overrides):
    return select_prompt_payload(
        FakeStore(), root=tmp_path, session_id="session-synthetic", state=state, explicit_domain=None,
        budget=budget, selector=selector_config(**jev_overrides), mode="jev",
    )


ON = {"enabled": True, "score_floor": 0.25, "rare_word_share": 0.5, "slots": 2, "keep": 30}
FILLER = tuple(record(f"filler{index}", f"No, that is wrong, the {index} banner stays.") for index in range(6))


@pytest.mark.parametrize("statement", [
    RULE,
    "Always use absolute paths.",
    "Never rename the folder.",
    "Whenever there is a release, always use the full version.",
    "Thanks, from now on keep the changelog short.",
    "Keep the changelog short from now on.",
    "Use plain text going forward.",
])
def test_a_sentence_built_as_a_rule_is_a_standing_rule(statement):
    assert rule_sentences(record("r", statement))


@pytest.mark.parametrize("statement", [
    "We can always change the live version later, no rush at all.",
    "No, that is never going to work on this page.",
    CORRECTION,
    "",
])
def test_a_mention_of_the_word_is_not_a_standing_rule(statement):
    assert rule_sentences(record("r", statement)) == []


def test_a_rule_past_what_jev_can_read_is_not_a_standing_rule():
    words = "This opens with a long account of the release and nothing else. " * 8
    assert rule_sentences(record("r", words + RULE)) == []


def test_a_statement_that_is_not_a_string_is_not_a_rule():
    assert rule_sentences(record("r", "x", judgment_statement=None)) == []


def test_a_captured_record_that_stopped_reading_as_feedback_is_stale():
    assert reads_as_feedback(record("r", STALE)) is False
    assert reads_as_feedback(record("r", CORRECTION)) is True
    assert reads_as_feedback(record("r", RULE)) is True


def test_a_reply_correction_is_not_stale():
    assert reads_as_feedback(record("r", "It should be the blue banner.")) is True


def test_only_captured_records_can_be_stale():
    assert reads_as_feedback(record("r", STALE, authority_tier="ratified_knowledge")) is True
    assert reads_as_feedback(record("r", "   ")) is True


@pytest.mark.parametrize("mechanism", ["explicit_cli", "approved_import", None])
def test_a_record_the_detector_never_saw_is_not_stale(mechanism):
    assert reads_as_feedback(record("r", STALE, capture_mechanism=mechanism)) is True


def test_only_a_capture_from_before_the_rules_changed_can_be_stale():
    fresh = stale_filter(RULES_CHANGED)
    assert fresh(record("old", STALE)) is False
    assert fresh(record("new", STALE, valid_from="2026-06-01T00:00:00Z")) is True
    assert fresh(record("offset", STALE, valid_from="2026-06-01T02:00:00+09:00")) is False


def test_a_correction_after_a_removed_quote_survives_the_check():
    stored = "Here is what you wrote:\n\nNo, keep the old one."
    assert stale_filter(RULES_CHANGED)(record("r", stored, valid_from="2026-07-01T00:00:00Z")) is True


def test_rare_matches_folds_plurals_and_reads_only_the_rule_sentence():
    rule = record("rule", "From now on, handoffs should name the owner.")
    other = record("other", "Never rename it. The handoff was late and the invoice too.")
    both = frozenset({rule.record_id, other.record_id})
    entries = [rule, other, *FILLER]
    assert rare_matches("write the handoff", entries, both, 0.5) == {rule.record_id}


def test_rare_matches_ignores_the_words_that_only_mark_a_rule():
    rule = record("rule", "Always keep the owner named.")
    assert rare_matches("why is it always slow", [rule], frozenset({rule.record_id}), 1.0) == frozenset()


def test_rare_matches_ignores_a_word_most_records_use():
    rule = record("rule", "From now on, name the owner of the invoice.")
    others = [record(f"o{index}", f"The invoice {index} was fine.") for index in range(9)]
    assert rare_matches("check the invoice", [rule, *others], frozenset({rule.record_id}), 0.5) == frozenset()


@pytest.mark.parametrize("size", [1, 3, 6, 7])
def test_a_word_only_the_rule_uses_is_rare_in_a_small_pool(size):
    rule = record("rule", "Always use the kumquat format for release notes.")
    others = [record(f"o{index}", f"No, that is wrong, the {index} banner stays.") for index in range(size - 1)]
    assert rare_matches("kumquat please", [rule, *others], frozenset({rule.record_id}), 0.15) == {rule.record_id}


def test_prefilter_keeps_a_rule_that_shares_no_word_with_the_turn():
    rule = record("rule", OFF_TOPIC_RULE)
    matches = [record(f"m{index}", f"release note {index}") for index in range(5)]
    kept = prefilter("release note", [*matches, rule], keep=3, standing=frozenset({rule.record_id}))
    assert len(kept) == 3
    assert rule in kept


def test_prefilter_is_unchanged_with_no_rule_and_no_check():
    entries = [record(f"m{index}", f"release note {index}") for index in range(5)]
    assert prefilter("release note", entries, keep=3) == prefilter("release note", entries, keep=3, standing=frozenset())


def test_prefilter_asks_only_about_records_it_would_keep():
    entries = [record(f"m{index}", f"release note {index}") for index in range(50)]
    asked: list[str] = []
    kept = prefilter("release note", entries, keep=3, accept=lambda item: asked.append(item.record_id) or True)
    assert len(kept) == 3
    assert len(asked) == 3


def test_prefilter_stops_asking_when_told_and_drops_what_it_did_not_reach():
    entries = [record(f"m{index}", f"release note {index}") for index in range(5)]
    asked: list[str] = []
    kept = prefilter(
        "release note", entries, keep=5, accept=lambda item: asked.append(item.record_id) or True,
        stop=lambda: len(asked) >= 2,
    )
    assert len(kept) == 2


def test_a_full_prefilter_cannot_be_overrun_by_rules():
    rules = [record(f"r{index}", f"Never rename folder {index}.") for index in range(5)]
    kept = prefilter("anything", rules, keep=2, standing=frozenset(item.record_id for item in rules))
    assert len(kept) == 2


def test_a_matching_rule_below_the_cutoff_leads_the_picks():
    rule, other = record("rule", RULE), record("other", CORRECTION)
    selector = JevSelector(settings(), transport=scored({RULE: 0.3, CORRECTION: 0.9}))
    result = selector.select("write the release note", [rule, other], standing=frozenset({rule.record_id}))
    assert [item.record_id for item, _ in result.picks] == [rule.record_id, other.record_id]
    assert result.reserved == {rule.record_id}


def test_a_rule_under_its_own_floor_earns_no_place():
    rule = record("rule", RULE)
    selector = JevSelector(settings(), transport=scored({RULE: 0.2}))
    result = selector.select("write the release note", [rule], standing=frozenset({rule.record_id}))
    assert result.picks == ()
    assert result.outcome == "no_picks"


def test_a_rule_with_no_rare_shared_word_stays_an_ordinary_candidate():
    rule = record("rule", OFF_TOPIC_RULE)
    selector = JevSelector(settings(), transport=scored({OFF_TOPIC_RULE: 0.3}))
    assert selector.select("write the release note", [rule], standing=frozenset({rule.record_id})).picks == ()
    picked = JevSelector(settings(), transport=scored({OFF_TOPIC_RULE: 0.6}))
    result = picked.select("write the release note", [rule], standing=frozenset({rule.record_id}))
    assert [item.record_id for item, _ in result.picks] == [rule.record_id]
    assert result.reserved == frozenset()


def test_only_the_slots_are_reserved_and_the_rest_keep_the_cutoff():
    rules = [record(f"r{index}", f"From now on, every release note should name owner {index}.") for index in range(3)]
    scores = {rules[0].judgment_statement: 0.3, rules[1].judgment_statement: 0.35, rules[2].judgment_statement: 0.26}
    selector = JevSelector(settings(), transport=scored(scores))
    result = selector.select(
        "write the release note", [*rules, *FILLER], standing=frozenset(item.record_id for item in rules),
    )
    assert [item.record_id for item, _ in result.picks] == [rules[1].record_id, rules[0].record_id]


def test_reserved_names_only_rules_that_stayed_in_the_picks():
    rules = [record(f"r{index}", f"From now on, every release note should name owner {index}.") for index in range(2)]
    selector = JevSelector(settings(top=1), transport=scored({rules[0].judgment_statement: 0.3, rules[1].judgment_statement: 0.35}))
    result = selector.select(
        "write the release note", [*rules, *FILLER], standing=frozenset(item.record_id for item in rules),
    )
    assert [item.record_id for item, _ in result.picks] == [rules[1].record_id]
    assert result.reserved == {rules[1].record_id}


def test_with_no_rules_picks_are_plain_score_order():
    first, second = record("a", RULE), record("b", CORRECTION)
    result = JevSelector(settings(), transport=scored({RULE: 0.5, CORRECTION: 0.9})).select("x y", [first, second])
    assert [item.record_id for item, _ in result.picks] == [second.record_id, first.record_id]
    assert result.reserved == frozenset()


def test_all_stale_is_no_picks_without_a_request():
    calls = []

    def transport(*_):
        calls.append(1)
        raise AssertionError("no request expected")

    result = JevSelector(settings(), transport=transport).select(
        "invoice totals", [record("stale", STALE)], accept=reads_as_feedback,
    )
    assert (result.outcome, result.withheld, calls) == ("no_picks", 1, [])


def test_with_no_rules_nothing_looks_for_rare_words(monkeypatch):
    monkeypatch.setattr(jev, "rare_matches", lambda *_: pytest.fail("rare_matches ran with no rules"))
    JevSelector(settings(), transport=scored({RULE: 0.9})).select("write the release note", [record("a", RULE)])


class Ticks:
    """A clock that moves only when told to, as a stale check takes time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_stale_check_stops_at_its_share_and_the_request_carries_what_it_judged():
    entries = [record(f"m{index}", f"release note {index}") for index in range(9)]
    clock = Ticks()
    sent = []

    def accept(item):
        clock.now += 0.5
        return True

    def transport(endpoint, body, headers, timeout):
        sent.append(len(json.loads(body)["questions"]))
        return scored({})(endpoint, body, headers, timeout)

    result = JevSelector(settings(timeout_seconds=3.0), transport=transport, clock=clock).select(
        "release note", entries, accept=accept,
    )
    assert sent == [2]
    assert result.outcome == "no_picks"
    assert result.scan_cut is True


def test_a_stale_check_that_reaches_nothing_in_time_is_a_timeout():
    clock = Ticks()

    def accept(item):
        clock.now += 2.0
        return False

    result = JevSelector(settings(timeout_seconds=3.0), transport=scored({}), clock=clock).select(
        "invoice totals", [record(f"s{index}", STALE) for index in range(3)], accept=accept,
    )
    assert (result.outcome, result.fallback_reason, result.withheld, result.scan_cut) == ("fallback", "timeout", 1, True)


def test_an_expired_deadline_never_starts_the_stale_check():
    asked = []
    result = JevSelector(settings(timeout_seconds=-1.0), transport=scored({})).select(
        "invoice totals", [record("s", STALE)], accept=lambda item: asked.append(item) or False,
    )
    assert (result.fallback_reason, asked) == ("timeout", [])


def test_with_no_stale_check_the_clock_is_not_read_per_record():
    calls = []

    def clock():
        calls.append(1)
        return 0.0

    entries = [record(f"m{index}", f"release note {index}") for index in range(40)]
    JevSelector(settings(), transport=scored({}), clock=clock).select("release note", entries)
    assert len(calls) < 10


def test_the_decision_line_reports_rules_and_stale_records_only_when_there_are_any():
    plain = decision_entry(SelectionResult(), mode="shadow", session_ref="s", receipt_scope=None)
    assert not {"reserved_picks", "stale_withheld", "stale_scan_cut"} & set(plain)
    full = decision_entry(
        SelectionResult(reserved=frozenset({"a"}), withheld=2, scan_cut=True),
        mode="shadow", session_ref="s", receipt_scope=None,
    )
    assert (full["reserved_picks"], full["stale_withheld"], full["stale_scan_cut"]) == (1, 2, True)


def test_off_by_default_nothing_is_withheld_or_reserved(tmp_path, records, monkeypatch):
    records.extend([record("rule", RULE), record("stale", STALE)])
    monkeypatch.setattr(jev, "http_post", scored({RULE: 0.3, STALE: 0.9}))
    decision = select(tmp_path, score_floor=0.4)
    assert decision.response["selected_ids"] == [records[1].record_id]
    stages = decision.response["selector_stages"]
    assert "reserved" not in stages and "stale_withheld" not in stages


def test_turned_on_the_rule_is_delivered_and_the_stale_record_is_not(tmp_path, records, monkeypatch):
    records.extend([record("rule", RULE), record("stale", STALE)])
    monkeypatch.setattr(jev, "http_post", scored({RULE: 0.3, STALE: 0.9}))
    decision = select(tmp_path, score_floor=0.4, stale_before=RULES_CHANGED, standing_rules=ON)
    assert decision.response["selected_ids"] == [records[0].record_id]
    stages = decision.response["selector_stages"]
    assert (stages["reserved"], stages["stale_withheld"]) == (1, 1)
    line = json.loads(decision.response["payload"].splitlines()[0])
    assert line["selector"]["reserved"] is True


def test_a_rule_already_delivered_is_not_reserved_again(tmp_path, records, monkeypatch):
    records.append(record("rule", RULE))
    records.append(record("other", CORRECTION))
    monkeypatch.setattr(jev, "http_post", scored({RULE: 0.3, CORRECTION: 0.9}))
    first = select(tmp_path, score_floor=0.4, standing_rules=ON)
    retrieval.commit_payload_delivery(
        root=tmp_path, session_id="session-synthetic", snapshot_id=first.response["snapshot_id"],
        domain_id=first.response["receipt_scope"],
    )
    second = select(tmp_path, state="write another release note", score_floor=0.4, standing_rules=ON)
    assert records[0].record_id not in second.response["selected_ids"]


def test_a_stale_record_is_not_delivered_locally_either(tmp_path, records, monkeypatch):
    records.extend([
        record("stale", STALE, ontology_type="Belief"), record("fine", CORRECTION, ontology_type="Belief"),
        record("sent", RULE),
    ])
    monkeypatch.setattr(jev, "http_post", scored({}))
    decision = select_prompt_payload(
        FakeStore(), root=tmp_path, session_id="session-synthetic", state="invoice totals", explicit_domain=None,
        budget=4096, selector=selector_config(node_types=["Verdict"], stale_before=RULES_CHANGED), mode="jev",
        local_sections=frozenset({"general"}),
    )
    assert records[0].record_id not in decision.response["selected_ids"]
    assert records[1].record_id in decision.response["selected_ids"]
    assert decision.response["selector_stages"]["stale_withheld"] == 1


def test_a_rule_that_reads_as_stale_is_not_sent_or_reserved(tmp_path, records, monkeypatch):
    block = "Here is the spec\n---\nIntro line here.\nAlways use tabs in generated files.\n" + "plain text\n" * 90
    statement = block + "---\nthanks for reading"
    records.append(record("rule", statement))
    assert rule_sentences(records[0]) and not reads_as_feedback(records[0])
    asked = []

    def transport(endpoint, body, headers, timeout):
        asked.append(json.loads(body)["questions"])
        return scored({})(endpoint, body, headers, timeout)

    monkeypatch.setattr(jev, "http_post", transport)
    decision = select(tmp_path, state="tabs in generated files", stale_before=RULES_CHANGED, standing_rules=ON)
    assert asked == []
    assert decision.response["selector_stages"]["stale_withheld"] == 1
    assert decision.response["selected_ids"] == []


def test_a_compact_reserved_rule_still_says_its_rule():
    opening = "Always rehearse database migrations on staging first."
    long = opening + " " + " ".join(f"Please also note item {index} is wrong and should change." for index in range(40))
    rule = record("rule", long)
    line = json.loads(render_compact(rule, selector={"reserved": True}, rules=rule_sentences(rule)))
    assert line["text"] == opening
    assert (line["text_basis"], line["text_complete"]) == ("rule_sentences", False)


def test_a_long_reserved_rule_cut_for_room_reaches_the_session(tmp_path, records, monkeypatch):
    opening = "Always rehearse database migrations on staging first."
    long = opening + " " + " ".join(f"Please also note item {index} is wrong and should change." for index in range(90))
    records.append(record("rule", long))
    records.append(record("other", "No, the migration banner is wrong. " + "It should stay as it was. " * 90))
    monkeypatch.setattr(jev, "http_post", scored({}, default=0.9))
    decision = select(tmp_path, state="rehearse the migrations", score_floor=0.4, standing_rules=ON)
    assert opening in decision.response["payload"]
    assert decision.response["selector_stages"]["reserved"] == 1


def test_the_reserved_stage_counts_rules_that_were_delivered():
    rule = record("rule", RULE)
    fits = retrieval._jev_response("s", "scope", [(rule, 0.3)], "picked", 1, 4096, reserved={rule.record_id: [RULE]})
    assert fits["selector_stages"]["reserved"] == 1
    no_room = retrieval._jev_response("s", "scope", [(rule, 0.3)], "picked", 1, 10, reserved={rule.record_id: [RULE]})
    assert "reserved" not in no_room["selector_stages"]


def test_the_newest_rules_are_chosen_by_the_time_they_mean(tmp_path, records, monkeypatch):
    earlier = record("earlier", "Never rename folder one.", valid_from="2026-10-31T06:00:00+09:00")
    later = record("later", "Never rename folder two.", valid_from="2026-10-30T23:00:00Z")
    records.extend([earlier, later])
    sent = []

    def transport(endpoint, body, headers, timeout):
        sent.append([item["instructions"] for item in json.loads(body)["questions"].values()])
        return scored({})(endpoint, body, headers, timeout)

    monkeypatch.setattr(jev, "http_post", transport)
    select(tmp_path, state="zzz", prefilter_keep=1, standing_rules={**ON, "keep": 1})
    assert sent == [[QUESTION_PREFIX + later.judgment_statement]]


def test_keep_bounds_how_many_rules_are_sent(tmp_path, records, monkeypatch):
    records.extend(record(f"r{index}", f"Never rename folder {index}.", valid_from=f"2026-01-{index + 1:02d}T00:00:00Z") for index in range(4))
    sent = []

    def transport(endpoint, body, headers, timeout):
        sent.append(len(json.loads(body)["questions"]))
        return scored({})(endpoint, body, headers, timeout)

    monkeypatch.setattr(jev, "http_post", transport)
    select(tmp_path, state="zzz", prefilter_keep=2, standing_rules={**ON, "keep": 1})
    assert sent == [2]


@pytest.mark.parametrize(("field", "value"), [
    ("stale_before", True),
    ("stale_before", "yesterday"),
    ("stale_before", "2026-13-01T00:00:00Z"),
    ("stale_before", "2026-01-31T12:00:00+09:00"),
    ("standing_rules", {**ON, "keep": 150}),
    ("standing_rules", {**ON, "extra": 1}),
    ("standing_rules", {**ON, "enabled": "yes"}),
    ("standing_rules", {**ON, "score_floor": 1.5}),
    ("standing_rules", {**ON, "rare_word_share": 0}),
    ("standing_rules", {**ON, "slots": 0}),
    ("standing_rules", {**ON, "slots": True}),
    ("standing_rules", {**ON, "keep": 151}),
])
def test_bad_settings_are_refused(tmp_path, field, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "selector": selector_config(**{field: value}),
    }))
    with pytest.raises(ValidationError):
        load_config(path)


def test_a_keep_at_or_above_prefilter_keep_is_fine_while_rules_are_off(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "selector": selector_config(prefilter_keep=5),
    }))
    assert load_config(path)["selector"]["jev"]["prefilter_keep"] == 5


def test_the_new_settings_default_off():
    jev_defaults = DEFAULTS["selector"]["jev"]
    assert jev_defaults["stale_before"] is None
    assert jev_defaults["standing_rules"]["enabled"] is False
