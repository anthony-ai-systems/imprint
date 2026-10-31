"""Selected judgments must reach the turn, and say so when they cannot."""
import json

import pytest

from imprint.retrieve import _jev_response
from imprint.retrieve.engine import COMPACT_TEXT_BYTES, _render, render_compact
from imprint.retrieve.jev import SelectionResult, decision_entry
from imprint.retrieve.models import RetrievalRecord, RetrievalResult

BUDGET = 4096
DEFAULT_BUDGET = 32768
BACKGROUND = "Some background about the project and how it came about. " * 12


def _record(record_id="urn:synthetic:judgment:1", *, text="Keep the source label.", summary="", evidence=("urn:synthetic:evidence:1",)):
    return RetrievalRecord(
        record_id=record_id, text=text, section="general", provenance_status="operator_authored",
        authority_tier="operator", evidence_ids=evidence, case_ids=("urn:synthetic:case:1",),
        case_summaries=(summary,) if summary else (), source_receipt_ids=("urn:synthetic:receipt:1",),
    )


def _response(picks, *, outcome="picked", budget=BUDGET, local=None, retried=frozenset()):
    return _jev_response("snapshot", "scope", picks, outcome, len(picks), budget, local, retried)


def _sized(index, size):
    return (_record(f"urn:synthetic:judgment:{index}", text="z" * size), 0.9 - index / 100)


def _rendered_exactly(size, *, rank=1, score=0.9):
    """A pick whose whole line at this rank is exactly ``size`` bytes."""
    selection = {"mode": "jev", "rank": rank, "score": score}
    overhead = len(_render(_record(text=""), selector=selection))
    return (_record(text="z" * (size - overhead)), score)


def _local(size, count=1):
    payload = b"x" * (size - 1) + b"\n"
    return RetrievalResult(
        payload=payload, selected_ids=tuple(f"urn:synthetic:local:{index}" for index in range(count)),
        eligible_count=count, omitted_count=0, selected_bytes=size, budget_bytes=BUDGET,
        section_bytes={"core": 0, "general": 0, "domain": size}, tokenizer_version="unused",
        authority_mode="authoritative", requested_partitions=(), selected_by_partition={},
    )


def _lines(response):
    return [json.loads(line) for line in response["payload"].splitlines()]


def test_small_pick_is_delivered_in_full():
    (line,) = _lines(_response([(_record(summary="A short case."), 0.9)]))
    assert "compact" not in line
    assert line["case_summaries"] == ["A short case."]


def test_large_pick_is_delivered_compact_with_its_references():
    record = _record(text="Lead with the judgment. " + "x" * 2000, summary="y" * 170000)
    response = _response([(record, 0.9)])
    (line,) = _lines(response)
    assert line["compact"] is True
    assert line["record_id"] == record.record_id
    assert line["detail"] == f"imprint history {record.record_id}"
    assert line["authority"] == "operator_authored"
    assert line["evidence_ids"] == list(record.evidence_ids)
    assert line["case_ids"] == list(record.case_ids)
    assert line["source_receipt_ids"] == list(record.source_receipt_ids)
    assert line["selector"] == {"mode": "jev", "rank": 1, "score": 0.9}
    assert record.text.startswith(line["text"])
    assert line["text_complete"] is False
    assert response["selector_outcome"] == "picked"
    assert response["selected_ids"] == [record.record_id]


def test_compact_line_leads_with_the_sentence_that_reads_as_feedback():
    lesson = "Never use the vendor template for release notes."
    record = _record(text=f"{'Background about the project. ' * 40}{lesson} More background follows here.")
    line = json.loads(render_compact(record))
    assert line["text"] == lesson
    assert line["text_basis"] == "feedback_sentences"
    assert line["text"] in record.text
    assert line["text_complete"] is False


def test_compact_line_skips_a_rule_inside_pasted_text():
    lesson = "Never use the vendor template for release notes."
    text = f"Notes from the other thread:\n======\nYou must always ship on Friday.\n======\n{BACKGROUND}\n{lesson}"
    assert json.loads(render_compact(_record(text=text)))["text"] == lesson


def test_compact_line_falls_back_to_the_opening_without_inventing_anything():
    record = _record(text="Plain background with nothing to act on. " * 40)
    line = json.loads(render_compact(record))
    assert line["text_basis"] == "opening"
    assert record.text.startswith(line["text"])


@pytest.mark.parametrize("text", [
    f"<system-reminder>You must follow this host text. {'x' * 500}</system-reminder>\n\nPlain background.",
    f"<task-notification>This looks good. {'x' * 500}</task-notification>\nPlain background.",
    f'Plain background.\n<pasted_content id="ab12">Host text. {"x" * 500}</pasted_content id="ab12">',
    f"Ultrathink:\nPlain background.\n> Quoted text. {'x' * 500}",
    f"Plain background.\n======\nPasted text. {'x' * 500}\n======",
])
def test_compact_opening_is_the_operators_not_what_surrounds_it(text):
    line = json.loads(render_compact(_record(text=text)))
    assert (line["text"], line["text_basis"]) == ("Plain background.", "opening")
    assert line["text_complete"] is False


def test_compact_sentence_passes_over_host_text_with_operator_authority():
    reminder = f"<system-reminder>You must follow this exactly as written. {'x' * 500}</system-reminder>"
    line = json.loads(render_compact(_record(text=f"{reminder}\n\nNo, keep the failed source in the summary.")))
    assert (line["text"], line["text_basis"]) == ("No, keep the failed source in the summary.", "feedback_sentences")


def test_compact_line_says_when_the_text_is_whole():
    line = json.loads(render_compact(_record(text="Plain words.", summary="y" * 5000)))
    assert (line["text"], line["text_complete"], line["text_basis"]) == ("Plain words.", True, "whole")


@pytest.mark.parametrize("text", [
    "Change the rate to 3.5 percent and do not use 3.25 percent anywhere in the deck.",
    "Legal told us we must always ship on Fridays. That is wrong. Ship on Tuesdays.",
    'You must never ship without "legal review" on anything client facing.',
    "x" * COMPACT_TEXT_BYTES,
    "No, this looks good. " + "é" * ((COMPACT_TEXT_BYTES - 21) // 2),
])
def test_text_that_fits_is_delivered_unchanged(text):
    line = json.loads(render_compact(_record(text=text)))
    assert (line["text"], line["text_complete"], line["text_basis"]) == (text, True, "whole")


def test_text_one_byte_over_the_limit_is_no_longer_whole():
    line = json.loads(render_compact(_record(text="x" * (COMPACT_TEXT_BYTES + 1))))
    assert (line["text_complete"], line["text_basis"]) == (False, "opening")


def test_the_limit_is_in_bytes_not_characters():
    text = "é" * (COMPACT_TEXT_BYTES - 100)
    line = json.loads(render_compact(_record(text=text)))
    assert (line["text_complete"], line["text_basis"]) == (False, "opening")
    assert len(line["text"].encode("utf-8")) <= COMPACT_TEXT_BYTES


def test_a_later_correction_survives_an_earlier_approval():
    approval, correction = "The headline looks good, ship it.", "But the pricing table is wrong, remove the third tier."
    line = json.loads(render_compact(_record(text=f"{approval} {BACKGROUND}{correction} More words here.")))
    assert line["text"] == f"{approval}\n{correction}"
    assert line["text_basis"] == "feedback_sentences"
    assert line["text_complete"] is False


def test_the_reviewed_case_keeps_the_correction_after_the_approval():
    text = (
        "The headline looks good, ship it. " + BACKGROUND
        + "But the pricing table is wrong, remove the third tier before this goes out."
    )
    line = json.loads(render_compact(_record(text=text)))
    assert line["text"] == (
        "The headline looks good, ship it.\n"
        "But the pricing table is wrong, remove the third tier before this goes out."
    )


@pytest.mark.parametrize("correction", [
    "However, the third tier has to go.",
    "That said, the footer needs another pass.",
    "Please remove the third tier.",
    "Replace the chart with the table.",
    "The totals are incorrect on the last page.",
    "The pricing table looks wrong to me.",
])
def test_a_correction_in_other_words_survives_too(correction):
    text = f"The headline looks good, ship it. {BACKGROUND}{correction} More words here."
    assert json.loads(render_compact(_record(text=text)))["text"] == f"The headline looks good, ship it.\n{correction}"


def test_a_rule_the_operator_rejected_is_not_delivered_alone():
    text = f"Legal told us we must always ship on Fridays. {BACKGROUND}That is wrong. Ship on Tuesdays."
    line = json.loads(render_compact(_record(text=text)))
    assert line["text"] == "Legal told us we must always ship on Fridays.\nThat is wrong."


def test_sentences_keep_document_order_whatever_the_rule_order():
    text = f"Wrong, that is the wrong card for this. {BACKGROUND}Later on, this looks good to me."
    line = json.loads(render_compact(_record(text=text)))
    assert line["text"] == "Wrong, that is the wrong card for this.\nLater on, this looks good to me."


def test_the_last_correction_survives_when_earlier_sentences_fill_the_limit():
    earlier = [f"Never use template number {index} for any of the release notes." for index in range(30)]
    correction = "That is wrong, all of it."
    line = json.loads(render_compact(_record(text=" ".join(earlier) + f" {BACKGROUND}{correction}")))
    kept = line["text"].split("\n")
    assert len(line["text"].encode("utf-8")) <= COMPACT_TEXT_BYTES
    assert kept[-1] == correction
    assert kept[0] == earlier[0] and kept[1] == earlier[1]
    assert len(kept) > 3


def test_a_sentence_too_long_for_the_limit_is_passed_over_for_those_that_fit():
    lesson, long_rule = "Never use the vendor template.", "You must keep " + "x" * COMPACT_TEXT_BYTES + " here."
    correction = "That is wrong, keep the label."
    line = json.loads(render_compact(_record(text=f"{lesson} {long_rule} {correction}")))
    assert line["text"] == f"{lesson}\n{correction}"


def test_a_long_sentence_does_not_end_the_search_for_shorter_ones():
    lesson, long_rule = "Never use the vendor template.", "You must keep " + "x" * COMPACT_TEXT_BYTES + " here."
    correction = "That is wrong, keep the label."
    line = json.loads(render_compact(_record(text=f"{long_rule} {lesson} {correction}")))
    assert line["text"] == f"{lesson}\n{correction}"


def test_the_line_break_between_sentences_counts_toward_the_limit():
    last = "That is wrong, keep the label."
    room = COMPACT_TEXT_BYTES - len(last)
    fits = "You must keep " + "x" * (room - len("You must keep .") - 1) + "."
    too_long = "You must keep " + "x" * (room - len("You must keep .")) + "."
    assert json.loads(render_compact(_record(text=f"{fits} {BACKGROUND}{last}")))["text"] == f"{fits}\n{last}"
    assert json.loads(render_compact(_record(text=f"{too_long} {BACKGROUND}{last}")))["text"] == last


def test_a_last_sentence_over_the_limit_is_cut_rather_than_lost():
    last = "You must keep " + "é" * COMPACT_TEXT_BYTES
    line = json.loads(render_compact(_record(text=f"Never use the vendor template. {last}")))
    assert len(line["text"].encode("utf-8")) <= COMPACT_TEXT_BYTES
    assert last.startswith(line["text"])
    assert line["text_complete"] is False


def test_compact_opening_uses_the_stripped_text_past_the_limit():
    text = f'<pasted_content id="1">Pasted words. {"x" * 500}</pasted_content id="1">\nPlain background.'
    line = json.loads(render_compact(_record(text=text)))
    assert (line["text"], line["text_basis"]) == ("Plain background.", "opening")


def test_compact_text_never_splits_a_character():
    line = json.loads(render_compact(_record(text="é" * 2000)))
    assert set(line["text"]) == {"é"}
    assert len(line["text"].encode("utf-8")) <= COMPACT_TEXT_BYTES


def test_one_large_record_does_not_crowd_out_the_next_picks():
    picks = [(_record(f"urn:synthetic:judgment:{index}", text="z" * 3000), 0.9 - index / 100) for index in range(4)]
    response = _response(picks)
    assert len(response["selected_ids"]) == 4
    assert response["selected_bytes"] <= BUDGET
    assert response["selector_stages"] == {
        "eligible": 4, "picked": 4, "delivered": 4, "delivered_compact": 4, "unfit": 0,
        "local_records": 0, "local_bytes": 0,
    }


@pytest.mark.parametrize("size", [DEFAULT_BUDGET // 4, DEFAULT_BUDGET // 2, DEFAULT_BUDGET - 1000])
def test_a_lone_pick_past_a_quarter_of_the_budget_stays_whole(size):
    response = _response([_sized(0, size)], budget=DEFAULT_BUDGET)
    (line,) = _lines(response)
    assert "compact" not in line
    assert len(line["text"]) == size
    assert response["retry_ids"] == []


def test_large_picks_that_all_fit_stay_whole():
    response = _response([_sized(0, 10000), _sized(1, 10000)], budget=DEFAULT_BUDGET)
    assert [line.get("compact") for line in _lines(response)] == [None, None]


def test_local_records_count_toward_whether_all_picks_fit():
    response = _response([_sized(0, 10000)], budget=DEFAULT_BUDGET, local=_local(25000))
    (line,) = [json.loads(line) for line in response["payload"].splitlines()[1:]]
    assert line["compact"] is True


def test_local_bytes_decide_whether_a_large_pick_can_stay_whole():
    picks = [_sized(0, 9000), _sized(1, 7500)]
    response = _response(picks, budget=DEFAULT_BUDGET, local=_local(16000))
    lines = [json.loads(line) for line in response["payload"].splitlines()[1:]]
    assert [bool(line.get("compact")) for line in lines] == [True, False]


def test_a_pick_at_exactly_a_quarter_of_the_budget_shares_it():
    pick, rival = _rendered_exactly(BUDGET // 4), _sized(1, 3000)
    assert [bool(line.get("compact")) for line in _lines(_response([pick, rival]))] == [False, True]


def test_a_pick_one_byte_past_a_quarter_of_the_budget_does_not():
    pick, rival = _rendered_exactly(BUDGET // 4 + 1), _sized(1, 3000)
    assert [bool(line.get("compact")) for line in _lines(_response([pick, rival]))] == [True, True]


def test_a_pick_past_a_quarter_of_the_budget_is_compacted_while_others_compete():
    picks = [_sized(0, 9000), _sized(1, 5000), _sized(2, 5000), _sized(3, 5000), _sized(4, 5000), _sized(5, 5000)]
    lines = _lines(_response(picks, budget=DEFAULT_BUDGET))
    assert [bool(line.get("compact")) for line in lines] == [True, False, False, False, False, False]


def test_a_pick_compacted_for_room_is_not_latched():
    picks = [_sized(index, 7000) for index in range(6)]
    response = _response(picks, budget=DEFAULT_BUDGET)
    assert len(response["selected_ids"]) == 6
    assert response["retry_ids"] == [record.record_id for record, _ in picks[4:]]


def test_a_large_pick_compacted_while_others_compete_is_not_latched():
    picks = [_sized(0, 20000), _sized(1, 20000)]
    response = _response(picks, budget=DEFAULT_BUDGET)
    assert response["retry_ids"] == [record.record_id for record, _ in picks]


def test_a_pick_exactly_the_size_of_the_budget_can_still_arrive_whole():
    pick, rival = _rendered_exactly(BUDGET), _sized(1, 100)
    assert _response([pick, rival])["retry_ids"] == [pick[0].record_id]
    assert _response([_rendered_exactly(BUDGET + 1), rival])["retry_ids"] == []


def test_a_pick_that_can_never_fit_whole_stays_latched():
    picks = [_sized(0, 40000), _sized(1, 1000)]
    response = _response(picks, budget=DEFAULT_BUDGET)
    assert [bool(line.get("compact")) for line in _lines(response)] == [True, False]
    assert response["retry_ids"] == []


def test_a_pick_cut_once_is_no_longer_held_back_for_size():
    picks = [_sized(0, 20000), _sized(1, 20000)]
    retried = frozenset(record.record_id for record, _ in picks)
    response = _response(picks, budget=DEFAULT_BUDGET, retried=retried)
    first, second = _lines(response)
    assert "compact" not in first
    assert second["compact"] is True
    assert response["retry_ids"] == [picks[1][0].record_id]


def test_a_cut_pick_is_still_compacted_when_the_budget_has_no_room():
    picks = [_sized(0, 9000), _sized(1, 9000)]
    response = _response(picks, budget=12000, retried=frozenset({picks[1][0].record_id}))
    assert [bool(line.get("compact")) for line in _lines(response)] == [True, False]
    assert response["selected_bytes"] <= 12000


def test_retried_picks_never_exceed_the_budget():
    picks = [_sized(index, 15000) for index in range(6)]
    retried = frozenset(record.record_id for record, _ in picks)
    assert _response(picks, budget=DEFAULT_BUDGET, retried=retried)["selected_bytes"] <= DEFAULT_BUDGET


@pytest.mark.parametrize("count", [1, 5, 20, 150])
def test_budget_is_never_exceeded(count):
    picks = [(_record(f"urn:synthetic:judgment:{index}", text="z" * 3000), 0.9) for index in range(count)]
    assert _response(picks)["selected_bytes"] <= BUDGET


def test_picks_that_cannot_fit_are_not_reported_as_irrelevant():
    record = _record(evidence=tuple(f"urn:synthetic:evidence:{index}" for index in range(200)))
    response = _response([(record, 0.9)])
    assert response["selector_outcome"] == "oversized_picks"
    assert response["selected_ids"] == []
    assert response["selector_stages"]["unfit"] == 1


def test_a_pick_only_local_records_crowded_out_is_not_called_oversized():
    response = _response([_sized(0, 100)], budget=1000, local=_local(900, 3))
    assert response["selector_outcome"] == "crowded_picks"
    assert response["selector_stages"] == {
        "eligible": 1, "picked": 1, "delivered": 0, "delivered_compact": 0, "unfit": 1,
        "local_records": 3, "local_bytes": 900,
    }


def test_a_pick_too_large_either_way_stays_oversized_beside_local_records():
    record = _record(evidence=tuple(f"urn:synthetic:evidence:{index}" for index in range(200)))
    response = _response([(record, 0.9)], local=_local(900))
    assert response["selector_outcome"] == "oversized_picks"


def test_a_compact_pick_fitting_beside_local_records_stays_picked():
    response = _response([_sized(0, 3000)], local=_local(900))
    assert response["selector_outcome"] == "picked"
    assert response["selector_stages"]["delivered_compact"] == 1


def test_nothing_picked_stays_no_picks():
    response = _response([], outcome="no_picks")
    assert response["selector_outcome"] == "no_picks"
    assert response["selector_stages"]["picked"] == 0


def test_decision_line_carries_counts_and_no_content():
    record = _record(text="Private judgment words.")
    result = SelectionResult(picks=((record, 0.9),), outcome="picked", request_sent=True, request_bytes=1234)
    entry = decision_entry(
        result, mode="jev", session_ref="session", receipt_scope="scope",
        stages=_response([(record, 0.9)])["selector_stages"],
    )
    assert entry["request_bytes"] == 1234
    assert entry["stages"]["delivered"] == 1
    assert "Private judgment" not in json.dumps(entry)


def test_decision_line_keeps_jevs_outcome_beside_the_delivery_outcome():
    result = SelectionResult(picks=((_record(), 0.9),), outcome="picked")
    entry = decision_entry(
        result, mode="jev", session_ref="session", receipt_scope="scope", delivery_outcome="oversized_picks",
    )
    assert (entry["outcome"], entry["delivery_outcome"]) == ("picked", "oversized_picks")
