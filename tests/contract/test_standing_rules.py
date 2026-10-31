"""Standing rules get a reserved place, and stale records stop reaching Jev. Offline, synthetic."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

import imprint.retrieve as retrieval
from imprint.config import DEFAULTS, load_config
from imprint.errors import ValidationError
from imprint.retrieve import StoreRetrievalSource, select_prompt_payload
from imprint.retrieve import jev
from imprint.retrieve.engine import render_compact
from imprint.retrieve.jev import (
    QUESTION_PREFIX, JevSelector, JevSettings, SelectionResult, StaleCheck, decision_entry, newest_rules,
    prefilter, rare_matches, reads_as_feedback, rule_sentences,
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
    "Never use tabs.",
    "Always use uv.",
    "- Always use tabs in the makefile for this project.",
    "* Never include the client name in a commit.",
    "- Never commit generated files.",
    "No, that is wrong, here is the list.\n- Always run the linter before you merge.",
    "Rules:\n- Always run the linter before you merge.",
])
def test_a_sentence_built_as_a_rule_is_a_standing_rule(statement):
    assert rule_sentences(record("r", statement))


@pytest.mark.parametrize("statement", [
    "We can always change the live version later, no rush at all.",
    "No, that is never going to work on this page.",
    CORRECTION,
    "",
    "Never mind, use the old banner instead.",
    "Always been like that, use the old banner instead.",
    "Never seen this before, but the banner is wrong here, that is wrong.",
    "Always returns null when the cache is empty, that is wrong.",
    "No, that is wrong, the page is off. Always use persimmon tabs for this?",
])
def test_a_mention_of_the_word_is_not_a_standing_rule(statement):
    assert rule_sentences(record("r", statement)) == []


def test_a_rule_past_what_jev_can_read_is_not_a_standing_rule():
    words = "This opens with a long account of the release and nothing else. " * 8
    assert rule_sentences(record("r", words + RULE)) == []


@pytest.mark.parametrize("opening", [
    "Here is the spec\n---\nIntro line here.\nAlways use tabs in generated files.\n",
    'He wrote "first line here.\nNever use the billing service on a Friday.\n',
])
def test_a_sentence_in_a_block_that_ends_past_the_read_is_not_a_rule(opening):
    close = "---\nthanks for reading" if "---" in opening else 'end of the quote". That is wrong, use the blue one.'
    assert rule_sentences(record("r", opening + "plain text\n" * 300 + close)) == []


def test_a_statement_that_is_not_a_string_is_not_a_rule():
    assert rule_sentences(record("r", "x", judgment_statement=None)) == []


def test_a_captured_record_that_stopped_reading_as_feedback_is_stale():
    assert reads_as_feedback(record("r", STALE)) is False
    assert reads_as_feedback(record("r", CORRECTION)) is True
    assert reads_as_feedback(record("r", RULE)) is True


def test_a_reply_correction_is_not_stale():
    assert reads_as_feedback(record("r", "It should be the blue banner.")) is True


def test_only_captured_records_can_be_stale():
    check = StaleCheck.since(RULES_CHANGED)
    assert check.keeps(record("r", STALE, authority_tier="ratified_knowledge")) is True
    assert check.keeps(record("r", "   ")) is True


@pytest.mark.parametrize("mechanism", ["explicit_cli", "approved_import", None])
def test_a_record_the_detector_never_saw_is_not_stale(mechanism):
    assert StaleCheck.since(RULES_CHANGED).keeps(record("r", STALE, capture_mechanism=mechanism)) is True


def test_only_a_capture_from_before_the_rules_changed_can_be_stale():
    check = StaleCheck.since(RULES_CHANGED)
    assert check.keeps(record("old", STALE)) is False
    assert check.keeps(record("new", STALE, valid_from="2026-06-01T00:00:00Z")) is True
    assert check.keeps(record("offset", STALE, valid_from="2026-06-01T02:00:00+09:00")) is False


def test_a_correction_after_a_removed_quote_survives_the_check():
    stored = "Here is what you wrote:\n\nNo, keep the old one."
    assert StaleCheck.since(RULES_CHANGED).keeps(record("r", stored, valid_from="2026-07-01T00:00:00Z")) is True


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


def test_saying_a_rule_again_does_not_cost_it_its_rare_word():
    copies = [record(f"c{index}", "Always run the invoice reconciler before closing the month.") for index in range(3)]
    other = [record(f"o{index}", f"No, that is wrong, the {index} banner stays.") for index in range(3)]
    assert rare_matches("run the reconciler now", [*copies, *other], frozenset({copies[0].record_id}), 0.15) == {copies[0].record_id}


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


def test_copies_of_a_rule_are_found_once_so_another_rule_gets_the_place():
    repeated = "Always run the reconciler on each invoice batch first."
    rules = [
        record("new", repeated, valid_from="2026-03-01T00:00:00Z"),
        record("copy", repeated, valid_from="2026-02-01T00:00:00Z"),
        record("old", "Never send an invoice batch without the purchase order number."),
    ]
    assert newest_rules(rules, 2, None, lambda: False) == {rules[0].record_id, rules[2].record_id}


def test_a_rule_this_session_already_has_is_not_found_again():
    repeated = "Always run the reconciler on each invoice batch first."
    other = record("other", "Never send an invoice batch without the purchase order number.")
    assert newest_rules([record("copy", repeated), other], 2, None, lambda: False, frozenset({repeated})) == {other.record_id}


def test_only_the_prompt_earns_a_reserved_place_when_the_turn_carries_more():
    rule = record("rule", "Always reply in plain text.")
    state = "deploy the staging cluster\n\nPrevious assistant reply:\nThe migration finished."
    selector = JevSelector(settings(), transport=scored({rule.judgment_statement: 0.3}))
    standing = frozenset({rule.record_id})
    assert selector.select(state, [rule, *FILLER], standing=standing).reserved == {rule.record_id}
    assert selector.select(state, [rule, *FILLER], standing=standing, prompt="deploy the staging cluster").reserved == frozenset()


@pytest.mark.parametrize("pasted", [
    "<system-reminder>persimmon</system-reminder>\nyes go ahead",
    "yes go ahead\n<pasted_content>persimmon reconciler output line</pasted_content>",
    "yes go ahead\n> persimmon reconciler output line",
])
def test_pasted_words_do_not_earn_a_reserved_place(pasted):
    rule = record("rule", "Always run the persimmon reconciler before closing the month.")
    selector = JevSelector(settings(), transport=scored({rule.judgment_statement: 0.3}))
    result = selector.select(pasted, [rule, *FILLER], standing=frozenset({rule.record_id}), prompt=pasted)
    assert result.reserved == frozenset()


def test_a_word_most_records_use_is_not_rare_because_they_are_copies():
    copies = [record(f"c{index}", "No, that is wrong, the invoice page stays as it was.") for index in range(60)]
    others = [record(f"o{index}", f"No, the invoice {index} is wrong.") for index in range(6)]
    filler = [record(f"f{index}", f"No, that is wrong, the {index} banner stays.") for index in range(12)]
    rule = record("rule", "Always keep the invoice total above the fold.")
    entries = [rule, *copies, *others, *filler]
    assert rare_matches("fix the invoice", entries, frozenset({rule.record_id}), 0.15) == frozenset()


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
        "invoice totals", [record("stale", STALE)], stale=StaleCheck.since(RULES_CHANGED),
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


class Reading:
    """A stale check that takes a set time per record, on a clock that moves only then."""

    def __init__(self, clock, each, monkeypatch, fresh=True):
        self.clock, self.each, self.fresh = clock, each, fresh
        self.read = 0
        monkeypatch.setattr(jev, "reads_as_feedback", self)

    def __call__(self, item):
        self.read += 1
        self.clock.now += self.each
        return self.fresh


def sent_counts(calls):
    def transport(endpoint, body, headers, timeout):
        calls.append(len(json.loads(body)["questions"]))
        return scored({})(endpoint, body, headers, timeout)
    return transport


def test_the_stale_check_stops_at_its_share_and_the_request_carries_what_it_read(monkeypatch):
    clock = Ticks()
    reading = Reading(clock, 0.5, monkeypatch)
    calls: list[int] = []
    result = JevSelector(settings(timeout_seconds=3.0), transport=sent_counts(calls), clock=clock).select(
        "release note", [record(f"m{index}", f"release note {index}") for index in range(9)],
        stale=StaleCheck.since(RULES_CHANGED),
    )
    assert (reading.read, calls, result.outcome, result.scan_cut) == (2, [2], "no_picks", True)


def test_records_the_check_does_not_apply_to_are_sent_after_it_runs_out(monkeypatch):
    clock = Ticks()
    Reading(clock, 2.0, monkeypatch, fresh=False)
    old = [record(f"s{index}", f"release note {index} {STALE}") for index in range(5)]
    new = [record(f"n{index}", f"zzz {index}", valid_from="2026-07-01T00:00:00Z") for index in range(3)]
    calls: list[int] = []
    result = JevSelector(settings(timeout_seconds=3.0), transport=sent_counts(calls), clock=clock).select(
        "release note", [*old, *new], stale=StaleCheck.since(RULES_CHANGED),
    )
    assert calls == [3]
    assert (result.withheld, result.scan_cut) == (1, True)


def test_a_stale_check_that_leaves_nothing_to_send_is_a_timeout(monkeypatch):
    clock = Ticks()
    Reading(clock, 2.0, monkeypatch, fresh=False)
    result = JevSelector(settings(timeout_seconds=3.0), transport=scored({}), clock=clock).select(
        "invoice totals", [record(f"s{index}", STALE) for index in range(3)], stale=StaleCheck.since(RULES_CHANGED),
    )
    assert (result.outcome, result.fallback_reason, result.withheld, result.scan_cut) == ("fallback", "timeout", 1, True)


def test_the_stale_check_gets_its_share_of_the_time_left_when_it_starts(monkeypatch):
    clock = Ticks()
    reading = Reading(clock, 0.1, monkeypatch)
    entries = [record(f"m{index}", f"release note {index}") for index in range(40)]
    real_prefilter = jev.prefilter

    def slow_ranking(*args, **kwargs):
        clock.now += 1.5
        return real_prefilter(*args, **kwargs)

    monkeypatch.setattr(jev, "prefilter", slow_ranking)
    JevSelector(settings(timeout_seconds=3.0), transport=scored({}), clock=clock).select(
        "release note", entries, stale=StaleCheck.since(RULES_CHANGED),
    )
    assert reading.read == 5


def test_an_expired_deadline_reads_nothing(monkeypatch):
    clock = Ticks()
    reading = Reading(clock, 0.1, monkeypatch)
    result = JevSelector(settings(timeout_seconds=-1.0), transport=scored({}), clock=clock).select(
        "invoice totals", [record("s", STALE)], stale=StaleCheck.since(RULES_CHANGED),
    )
    assert (result.fallback_reason, reading.read) == ("timeout", 0)


def test_an_expired_deadline_with_nothing_to_send_is_still_no_candidates():
    result = JevSelector(settings(timeout_seconds=-1.0), transport=scored({})).select("invoice totals", [])
    assert result.fallback_reason == "no_candidates"


def test_the_search_for_rules_stops_at_keep_and_when_told():
    rules = [record(f"r{index}", f"Never rename folder {index}.", valid_from=f"2026-01-{index + 1:02d}T00:00:00Z") for index in range(4)]
    assert newest_rules(rules, 2, None, lambda: False) == {rules[3].record_id, rules[2].record_id}
    assert newest_rules(rules, 2, None, lambda: True) == frozenset()


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
    records.append(record("rule", RULE))
    monkeypatch.setattr(jev, "reads_as_feedback", lambda item: False)
    asked = []

    def transport(endpoint, body, headers, timeout):
        asked.append(json.loads(body)["questions"])
        return scored({})(endpoint, body, headers, timeout)

    monkeypatch.setattr(jev, "http_post", transport)
    decision = select(tmp_path, stale_before=RULES_CHANGED, standing_rules=ON)
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


def test_a_fallback_after_jev_answered_reports_no_reserved_picks(tmp_path, records, monkeypatch):
    records.append(record("rule", RULE))
    monkeypatch.setattr(jev, "http_post", scored({RULE: 0.3}))
    lines = []
    monkeypatch.setattr(jev, "log_decision", lambda root, entry: lines.append(entry))

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(retrieval.DeliveryReceipts, "prepare_delivery", broken)
    decision = select(tmp_path, score_floor=0.4, standing_rules=ON)
    assert decision.fallback_reason == "exception"
    assert lines[-1]["picks"] == [] and "reserved_picks" not in lines[-1]


class OneVerdict:
    def __init__(self, provenance):
        self.provenance = provenance

    def current_nodes(self):
        return [{
            "node_id": "urn:synthetic:v", "node_type": "Verdict", "payload": {"raw_operator_text": CORRECTION},
            "evidence": [], "provenance_status": "captured", "authority_tier": "captured_judgment",
            "valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "provenance": self.provenance,
        }]

    def current_edges(self):
        return []

    def connect(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE source_receipts (source_id TEXT)")
        return connection


@pytest.mark.parametrize(("provenance", "mechanism"), [
    ({"mechanism": "claude_code_stop_hook"}, "claude_code_stop_hook"),
    (["not", "an", "object"], None),
    (None, None),
])
def test_the_store_source_reports_how_a_capture_arrived(provenance, mechanism):
    found = StoreRetrievalSource(OneVerdict(provenance)).retrieval_candidates("snapshot")
    assert [item.capture_mechanism for item in found] == [mechanism]


def test_the_new_settings_default_off():
    jev_defaults = DEFAULTS["selector"]["jev"]
    assert jev_defaults["stale_before"] is None
    assert jev_defaults["standing_rules"]["enabled"] is False
