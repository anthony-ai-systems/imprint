from copy import deepcopy

import pytest

from imprint.capture.detector import detect_explicit_feedback
from imprint.capture.schema import build_capture_envelope, new_urn
from imprint.derive.proposals import build_reason_addition_proposal, route_capture_to_proposal, validate_proposal
from imprint.errors import ValidationError


def captured(text="No, use the smaller card.", *, reason=None, status="absent"):
    return build_capture_envelope(
        operator_id=new_urn("operator"), session_id=new_urn("session"), node_id="node-gamma",
        case_description="A synthetic card was reviewed.", raw_operator_text=text, call_type="correct",
        capture_mechanism="explicit_cli", captured_by="contract/3.0.0",
        reason=reason, reason_status=status, chosen_alternatives=["small"], rejected_alternatives=["large"],
    )


@pytest.mark.parametrize("text, expected", [
    ("No, use the smaller card.", "correction_without_reason"),
    ("I prefer the smaller card.", "preference"),
    ("We must preserve labels.", "standard"),
    ("Approved. Ship it.", "approval"),
    ("Do not publish it.", "refusal"),
])
def test_deterministic_routes(text, expected):
    value = captured(text)
    proposal = route_capture_to_proposal(value, detect_explicit_feedback(text, prior_assistant_output="draft"))
    assert proposal["proposal_type"] == expected
    assert proposal["references"]["verdict_id"] == value["verdict"]["verdict_id"]
    assert proposal["payload"]["reason"] is None


def test_correction_with_why_uses_only_captured_reason():
    value = captured("Use the smaller card because it scans faster.", reason="it scans faster", status="supplied")
    detection = detect_explicit_feedback("No, use the smaller card.")
    proposal = route_capture_to_proposal(value, detection)
    assert proposal["proposal_type"] == "correction_with_reason"
    assert proposal["payload"]["reason"] == "it scans faster"


def test_later_reason_addition_preserves_original_null():
    original = captured()
    later = captured("The reason is that the smaller card scans faster.", reason="the smaller card scans faster", status="later_added")
    proposal = build_reason_addition_proposal(original, later)
    assert original["verdict"]["reason"] is None
    assert proposal["proposal_type"] == "reason_addition"
    assert proposal["payload"]["original_verdict_id"] == original["verdict"]["verdict_id"]


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(proposed_transition="captured"),
    lambda p: p.update(proposed_transition="ratified"),
    lambda p: p.update(proposal_type="purge"),
    lambda p: p["payload"].update(sql="DELETE FROM events"),
    lambda p: p["payload"].update(output_path="/tmp/escape"),
    lambda p: p["payload"].update(destination="C:\\escape\\state.json"),
    lambda p: p.update(unknown="field"),
])
def test_model_authority_escalation_is_rejected(mutation):
    value = captured()
    proposal = route_capture_to_proposal(value, detect_explicit_feedback(value["verdict"]["raw_operator_text"]))
    bad = deepcopy(proposal)
    mutation(bad)
    with pytest.raises(ValidationError):
        validate_proposal(bad)


@pytest.mark.parametrize("text", [
    "No, delete the opening sentence.",
    "No, update the heading to name the benefit.",
    "No, drop the vague language.",
    "I prefer to insert a concrete example here.",
    "No, alter the order of these paragraphs.",
])
def test_editorial_feedback_is_not_mistaken_for_sql(text):
    value = captured("No, use the smaller card.", reason=text, status="supplied")
    proposal = route_capture_to_proposal(value, detect_explicit_feedback(text))
    assert proposal["payload"]["reason"] == text
    assert proposal["provenance"]["authority_tier"] == "inferred_candidate"


@pytest.mark.parametrize("text", [
    "DELETE FROM events",
    "INSERT INTO events VALUES (1)",
    "UPDATE events SET payload = 'x'",
    "UPDATE main.events SET payload = 'x'",
    'UPDATE "events" SET payload = \'x\'',
    "INSERT OR REPLACE INTO events VALUES (1)",
    "UPDATE OR IGNORE events SET payload = 'x'",
    "UPDATE events AS e SET payload = 'x'",
    "UPDATE events e SET payload = 'x'",
    "UPDATE events SET (payload, kind) = ('x', 'y')",
    "UPDATE events INDEXED BY event_index SET payload = 'x'",
    "UPDATE events NOT INDEXED SET payload = 'x'",
    "DROP TABLE events",
    "DROP DATABASE imprint",
    "ALTER TABLE events ADD COLUMN value TEXT",
])
def test_sql_in_feedback_remains_rejected(text):
    value = captured("No, use the smaller card.", reason=text, status="supplied")
    with pytest.raises(ValidationError, match="forbidden transition or SQL"):
        route_capture_to_proposal(value, detect_explicit_feedback(text))
