"""Selected judgments must reach the turn, and say so when they cannot."""
import json

import pytest

from imprint.retrieve import _jev_response
from imprint.retrieve.engine import COMPACT_TEXT_BYTES, render_compact
from imprint.retrieve.jev import SelectionResult, decision_entry
from imprint.retrieve.models import RetrievalRecord

BUDGET = 4096


def _record(record_id="urn:synthetic:judgment:1", *, text="Keep the source label.", summary="", evidence=("urn:synthetic:evidence:1",)):
    return RetrievalRecord(
        record_id=record_id, text=text, section="general", provenance_status="operator_authored",
        authority_tier="operator", evidence_ids=evidence, case_ids=("urn:synthetic:case:1",),
        case_summaries=(summary,) if summary else (), source_receipt_ids=("urn:synthetic:receipt:1",),
    )


def _response(picks, *, outcome="picked", budget=BUDGET):
    return _jev_response("snapshot", "scope", picks, outcome, len(picks), budget)


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
    assert line["text_basis"] == "feedback_sentence"
    assert line["text"] in record.text
    assert line["text_complete"] is False


def test_compact_line_skips_a_rule_inside_pasted_text():
    lesson = "Never use the vendor template for release notes."
    record = _record(text=f"Notes from the other thread:\n======\nYou must always ship on Friday.\n======\n{lesson}")
    assert json.loads(render_compact(record))["text"] == lesson


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
    assert (line["text"], line["text_basis"]) == ("No, keep the failed source in the summary.", "feedback_sentence")


def test_compact_line_says_when_the_text_is_whole():
    line = json.loads(render_compact(_record(text="Plain words.", summary="y" * 5000)))
    assert (line["text"], line["text_complete"]) == ("Plain words.", True)


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
    }


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
