import json

import pytest

from imprint.capture.provenance import (
    NO_OPERATOR_MESSAGE_REASON,
    SYNTHETIC_ENTRY_REASON,
    classify_entry_provenance,
)
from imprint.capture.transcript import parse_native_stop_transcript
from imprint.cli import _parse_large_native_transcript

SKILL_BODY = (
    "Base directory for this skill: /Users/operator/.claude/skills/capture\n"
    "Read the SKILL.md file before doing anything else in this session."
)
TASK_NOTIFICATION = (
    "<task-notification>Agent scout finished its unbounded read of the repository."
    "</task-notification>"
)
SYSTEM_REMINDER = (
    "<system-reminder>The task tools have not been used recently.</system-reminder>"
)
DISTILLER_PROMPT = (
    "You are a claim distiller. Reduce the operator statement below to one "
    "unconditional claim and do not add anything the operator did not say."
)
# Written by the host into the user stream when a tool call is interrupted. It
# carries no origin, no promptSource, and no structural tell, so only the
# absence of a positive operator tell can keep it out.
INTERRUPTION_NOTICE = "[Request interrupted by user for tool use]"


def _transcript(path, entries) -> str:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    return str(path)


def _user(text: str, **fields) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **fields}


def _prompt(text: str, **fields) -> dict:
    """A submitted prompt as the host records it, without an origin field."""
    return _user(text, promptSource="typed", **fields)


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


def test_human_origin_is_operator_authored():
    entry = _user("No, keep the failed source.", origin={"kind": "human"})
    verdict = classify_entry_provenance(entry, "No, keep the failed source.")
    assert verdict.is_operator and verdict.basis == "origin"


@pytest.mark.parametrize("kind", ["task-notification", "hook", "agent"])
def test_non_human_origin_is_never_operator_authored(kind):
    # Origin decides alone: even feedback-shaped text stays out when the host
    # says a machine wrote it.
    entry = _user("No, that is wrong.", origin={"kind": kind})
    verdict = classify_entry_provenance(entry, "No, that is wrong.")
    assert verdict.is_operator is False and verdict.basis == "origin"


def test_prompt_source_is_the_positive_tell_when_origin_is_absent():
    # Real prompts carry promptSource on CLI versions that record no origin.
    entry = _prompt("No, restore the neutral heading.")
    verdict = classify_entry_provenance(entry, "No, restore the neutral heading.")
    assert verdict.is_operator and verdict.basis == "promptSource"


def test_prompt_source_entries_are_never_dropped_by_an_injected_marker():
    # The host injects reminders into real prompt entries, so consulting the
    # marker blacklist here would drop operator speech.
    text = "No.\n\n" + SYSTEM_REMINDER * 8
    verdict = classify_entry_provenance(_prompt(text), text)
    assert verdict.is_operator and verdict.basis == "promptSource"


@pytest.mark.parametrize("fields", [
    {"toolUseResult": {"stdout": "ok"}}, {"isSidechain": True}, {"isMeta": True},
])
def test_structural_tells_are_synthetic(fields):
    entry = _prompt("No, that is wrong.", **fields)
    verdict = classify_entry_provenance(entry, "No, that is wrong.")
    assert verdict.is_operator is False and verdict.basis == "structure"


@pytest.mark.parametrize("text", [
    SKILL_BODY, TASK_NOTIFICATION, SYSTEM_REMINDER, DISTILLER_PROMPT, INTERRUPTION_NOTICE,
])
def test_entries_without_any_operator_tell_are_synthetic(text):
    # No origin and no promptSource means the entry is not a submitted prompt
    # at all, whatever its text says.
    assert classify_entry_provenance(_user(text), text).is_operator is False


@pytest.mark.parametrize("text", [SKILL_BODY, TASK_NOTIFICATION, SYSTEM_REMINDER, DISTILLER_PROMPT])
def test_marker_basis_stays_reportable_for_recognisable_host_text(text):
    # The blacklist no longer decides eligibility, but it still names why the
    # entry was dropped so marker-basis drops remain auditable.
    assert classify_entry_provenance(_user(text), text).basis == "marker"


def test_marker_dominance_ignores_a_trailing_mention():
    text = (
        "No, drop the second heading and keep the source labels, because a reader "
        "who loses the labels cannot check the claim at all. "
        "The phrase <system-reminder> is only discussed here, not injected."
    )
    # Dropped for want of a positive tell, not misattributed to the marker.
    assert classify_entry_provenance(_user(text), text).basis == "structure"


def test_transcript_prefers_the_last_operator_entry_over_later_synthetic_ones(tmp_path):
    path = _transcript(tmp_path / "transcript.jsonl", [
        _assistant("I omitted one failed source."),
        _user("No, report every failed source explicitly.", origin={"kind": "human"}),
        _assistant("Reporting all sources."),
        _user(TASK_NOTIFICATION, origin={"kind": "task-notification"}),
        _user(INTERRUPTION_NOTICE),
        _user(SKILL_BODY),
    ])
    parsed = parse_native_stop_transcript(path)
    assert parsed["skip_reason"] is None
    assert parsed["operator_text"].startswith("No, report every failed source")


def test_synthetic_only_transcript_is_skipped_and_never_raises(tmp_path):
    path = _transcript(tmp_path / "transcript.jsonl", [
        _assistant("Working."),
        _user(TASK_NOTIFICATION, origin={"kind": "task-notification"}),
        _user("No, that is wrong.", isSidechain=True),
        _user(SKILL_BODY),
    ])
    parsed = parse_native_stop_transcript(path)
    assert parsed["skip_reason"] == SYNTHETIC_ENTRY_REASON
    assert parsed["provenance_basis"] == "marker"
    assert parsed["operator_text"] is None


def test_tool_result_only_transcript_is_skipped_and_never_raises(tmp_path):
    # Blank-text user entries once bypassed the gate and reached the raise that
    # exits 2 and blocks the host session's stop.
    path = _transcript(tmp_path / "transcript.jsonl", [
        _assistant("Running the suite."),
        {
            "type": "user",
            "toolUseResult": {"stdout": "419 passed"},
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "419 passed"},
            ]},
        },
    ])
    parsed = parse_native_stop_transcript(path)
    assert parsed["skip_reason"] == SYNTHETIC_ENTRY_REASON
    assert parsed["operator_text"] is None


def test_transcript_without_any_user_entry_is_skipped_and_never_raises(tmp_path):
    path = _transcript(tmp_path / "transcript.jsonl", [_assistant("Working.")])
    parsed = parse_native_stop_transcript(path)
    assert parsed["skip_reason"] == NO_OPERATOR_MESSAGE_REASON
    assert parsed["provenance_basis"] is None
    assert parsed["operator_text"] is None


def test_bounded_tail_applies_the_same_gate_without_raising(tmp_path):
    path = _transcript(tmp_path / "transcript.jsonl", [
        _assistant("Working."),
        _user(DISTILLER_PROMPT),
        _user(TASK_NOTIFICATION, origin={"kind": "task-notification"}),
    ])
    parsed = _parse_large_native_transcript(path)
    assert parsed["skip_reason"] == SYNTHETIC_ENTRY_REASON
    assert "degradation" not in parsed


def test_bounded_tail_without_any_user_entry_is_skipped_and_never_raises(tmp_path):
    path = _transcript(tmp_path / "transcript.jsonl", [_assistant("Working.")])
    parsed = _parse_large_native_transcript(path)
    assert parsed["skip_reason"] == NO_OPERATOR_MESSAGE_REASON
    assert "degradation" not in parsed


def test_parse_carries_the_source_entry_uuid_for_idempotent_capture(tmp_path):
    path = _transcript(tmp_path / "transcript.jsonl", [
        _assistant("I omitted one failed source."),
        _prompt("No, report every failed source explicitly.", uuid="entry-uuid-1"),
    ])
    assert parse_native_stop_transcript(path)["source_entry_id"] == "entry-uuid-1"
    assert _parse_large_native_transcript(path)["source_entry_id"] == "entry-uuid-1"
