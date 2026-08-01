import json

import pytest

from imprint.capture.provenance import SYNTHETIC_ENTRY_REASON, classify_entry_provenance
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


def _transcript(path, entries) -> str:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    return str(path)


def _user(text: str, **fields) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **fields}


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


def test_legacy_entry_without_origin_stays_eligible():
    entry = _user("No, restore the neutral heading.")
    verdict = classify_entry_provenance(entry, "No, restore the neutral heading.")
    assert verdict.is_operator and verdict.basis == "unmarked"


@pytest.mark.parametrize("fields", [
    {"toolUseResult": {"stdout": "ok"}}, {"isSidechain": True}, {"isMeta": True},
])
def test_legacy_structural_tells_are_synthetic(fields):
    entry = _user("No, that is wrong.", **fields)
    verdict = classify_entry_provenance(entry, "No, that is wrong.")
    assert verdict.is_operator is False and verdict.basis == "structure"


@pytest.mark.parametrize("text", [SKILL_BODY, TASK_NOTIFICATION, SYSTEM_REMINDER, DISTILLER_PROMPT])
def test_marker_blacklist_is_the_last_resort_for_legacy_entries(text):
    verdict = classify_entry_provenance(_user(text), text)
    assert verdict.is_operator is False and verdict.basis == "marker"


def test_marker_dominance_ignores_a_trailing_mention():
    text = (
        "No, drop the second heading and keep the source labels, because a reader "
        "who loses the labels cannot check the claim at all. "
        "The phrase <system-reminder> is only discussed here, not injected."
    )
    assert classify_entry_provenance(_user(text), text).is_operator


def test_transcript_prefers_the_last_human_entry_over_later_synthetic_ones(tmp_path):
    path = _transcript(tmp_path / "transcript.jsonl", [
        _assistant("I omitted one failed source."),
        _user("No, report every failed source explicitly.", origin={"kind": "human"}),
        _assistant("Reporting all sources."),
        _user(TASK_NOTIFICATION, origin={"kind": "task-notification"}),
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
