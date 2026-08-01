from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from imprint.store import ImprintStore


def _run(repo: Path, config: Path, action: str, event: dict) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["IMPRINT_CONFIG"] = str(config)
    return subprocess.run(
        [sys.executable, "-m", "imprint.cli", "hook", action],
        input=json.dumps(event), text=True, capture_output=True, cwd=repo, env=env, check=False,
    )


def test_hook_capture_compile_retrieve_and_once_delivery(tmp_path):
    repo = Path(__file__).parents[2]
    data = tmp_path / "data root with spaces"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(data),
        "operator_slug": "test-operator", "node_id": "test-node", "compiler": True,
        "context_budget_bytes": 32768,
    }))
    event = {
        "session_id": "hook-session-1",
        "operator_text": "No, report the failed source explicitly because missing evidence changes the conclusion.",
        "case_description": "Reviewing a multi-source synthesis",
    }
    captured = _run(repo, config, "stop-capture", event)
    assert captured.returncode == 0, captured.stderr
    receipt = json.loads(captured.stdout)
    assert receipt["status"] == "queued"
    assert receipt["canonical_status"] == "compiled"
    assert receipt["compile"] == {"captured": 1, "duplicate": 0, "quarantined": 0}

    store = ImprintStore(data / "test-operator" / "imprint.db")
    evidence_id = store.current_nodes(["Evidence"])[0]["node_id"]
    rule_id = store.append_derived_node(
        node_type="Rule",
        payload={"statement": "Cite every failed source in the research domain.", "domain_id": "research"},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[evidence_id], operator_id=store.current_nodes(["Verdict"])[0]["operator_id"],
        valid_from="2026-07-14T12:00:00Z", proposed_by="integration-test",
    )
    store.ratify_node(rule_id, ratifier="synthetic-operator")
    config_value = json.loads(config.read_text())
    config_value["domains"] = [{
        "domain_id": "research", "public_label": "Research",
        "safe_paths": ["Projects/Research"], "keywords": ["sources"], "frozen": False,
    }]
    config.write_text(json.dumps(config_value))

    first = _run(repo, config, "session-start", {"session_id": "fresh-session"})
    assert first.returncode == 0, first.stderr
    first_body = json.loads(first.stdout)
    assert first_body["status"] == "delivered"
    assert "failed source" in first_body["hookSpecificOutput"]["additionalContext"]
    second = _run(repo, config, "session-start", {"session_id": "fresh-session"})
    assert json.loads(second.stdout)["status"] == "already_delivered"

    domain = _run(repo, config, "user-prompt-submit", {
        "session_id": "fresh-session", "cwd": "Projects/Research/Current", "prompt": "review",
    })
    assert domain.returncode == 0, domain.stderr
    domain_body = json.loads(domain.stdout)
    assert domain_body["domain_id"] == "research"
    assert domain_body["selection_method"] == "path"
    context = domain_body["hookSpecificOutput"]["additionalContext"]
    assert "research domain" in context
    assert "missing evidence changes" not in context
    domain_again = _run(repo, config, "user-prompt-submit", {
        "session_id": "fresh-session", "cwd": "Projects/Research/Current", "prompt": "review",
    })
    assert json.loads(domain_again.stdout)["status"] == "already_delivered"

    assert store.integrity_check() == "ok"
    verdict = store.current_nodes(["Verdict"])[0]
    assert verdict["payload"]["reason"] is None
    assert verdict["payload"]["reason_status"] == "absent"


def test_stop_hook_without_feedback_text_is_honest_noop(tmp_path):
    repo = Path(__file__).parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "operator_slug": "test"}))
    result = _run(repo, config, "stop-capture", {"session_id": "s"})
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "hook_schema_version": "1.0.0",
        "reason": "feedback_text_unavailable",
        "status": "skipped",
    }


def test_native_claude_stop_payload_mines_bounded_transcript(tmp_path):
    repo = Path(__file__).parents[2]
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "primary",
    }))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I omitted one failed source."}]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "No, explicitly report every failed source because omission changes the decision."}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Understood."}}),
    ]) + "\n")
    result = _run(repo, config, "stop-capture", {
        "hook_event_name": "Stop", "session_id": "native-session",
        "transcript_path": str(transcript), "cwd": str(tmp_path),
        "stop_hook_active": False,
    })
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "queued"
    assert receipt["canonical_status"] == "compiled"
    assert receipt["compile"]["captured"] == 1
    store = ImprintStore(data / "test" / "imprint.db")
    verdict = store.current_nodes(["Verdict"])[0]
    assert verdict["payload"]["raw_operator_text"].startswith("No, explicitly report")
    assert len(verdict["evidence"]) == 2


SKILL_BODY = (
    "Base directory for this skill: /Users/operator/.claude/skills/capture\n"
    "Read SKILL.md before doing anything else. No, do not skip this step."
)
TASK_NOTIFICATION = (
    "<task-notification>Agent scout finished. No, the earlier answer was wrong."
    "</task-notification>"
)


def _stop_capture_over_transcript(tmp_path, entries) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo = Path(__file__).parents[2]
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "primary",
    }))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    result = _run(repo, config, "stop-capture", {
        "hook_event_name": "Stop", "session_id": "provenance-session",
        "transcript_path": str(transcript), "cwd": str(tmp_path),
        "stop_hook_active": False,
    })
    return result, data


def test_stop_capture_records_human_origin_feedback(tmp_path):
    result, data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "I omitted one failed source."}},
        {
            "type": "user", "origin": {"kind": "human"},
            "message": {"content": "No, report every failed source explicitly."},
        },
    ])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "queued"
    spooled = sorted((data / "test" / "spool" / "primary").glob("*.json"))
    assert len(spooled) == 1
    assert "report every failed source" in spooled[0].read_text()


@pytest.mark.parametrize("entry", [
    # Declared machine provenance decides alone, even on feedback-shaped text.
    {"type": "user", "origin": {"kind": "task-notification"},
     "message": {"content": TASK_NOTIFICATION}},
    # Legacy sessions carry no origin, so the structural tells decide.
    {"type": "user", "isSidechain": True,
     "message": {"content": "No, that heading is wrong."}},
    {"type": "user", "toolUseResult": {"stdout": "ok"},
     "message": {"content": "No, that heading is wrong."}},
    # Marker blacklist, consulted only when origin and structure are silent.
    {"type": "user", "message": {"content": SKILL_BODY}},
])
def test_stop_capture_skips_synthetic_transcript_entries(tmp_path, entry):
    result, data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "Working."}}, entry,
    ])
    # The skip path must stay exit 0: exit 2 from a Stop hook blocks the host.
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "hook_schema_version": "1.0.0",
        "reason": "synthetic_transcript_entry",
        "status": "skipped",
    }
    assert not list((data / "test" / "spool").rglob("*.json"))


def test_stop_capture_reaches_past_synthetic_entries_to_the_operator_turn(tmp_path):
    result, data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "I omitted one failed source."}},
        {
            "type": "user", "origin": {"kind": "human"},
            "message": {"content": "No, report every failed source explicitly."},
        },
        {"type": "assistant", "message": {"content": "Reporting all sources."}},
        {"type": "user", "origin": {"kind": "task-notification"},
         "message": {"content": TASK_NOTIFICATION}},
        {"type": "user", "message": {"content": SKILL_BODY}},
    ])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "queued"
    spooled = sorted((data / "test" / "spool" / "primary").glob("*.json"))
    assert len(spooled) == 1
    body = spooled[0].read_text()
    assert "report every failed source" in body
    assert "Base directory for this skill" not in body


def test_hook_rejects_wrong_event_contract(tmp_path):
    repo = Path(__file__).parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "operator_slug": "test"}))
    result = _run(repo, config, "stop-capture", {
        "hook_schema_version": "9.0.0", "hook_event_name": "SessionStart",
    })
    assert result.returncode == 2
    assert json.loads(result.stdout)["error_type"] == "ValidationError"
