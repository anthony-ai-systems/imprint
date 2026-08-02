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
        json.dumps({"type": "user", "promptSource": "typed", "message": {"role": "user", "content": "No, explicitly report every failed source because omission changes the decision."}}),
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
SYSTEM_REMINDER = (
    "<system-reminder>The task tools have not been used recently. Consider "
    "whether they apply. This is a gentle reminder.</system-reminder>"
)
INTERRUPTION_NOTICE = "[Request interrupted by user for tool use]"


def _stop_capture_over_transcript(
    tmp_path, entries, *, runs: int = 1,
) -> tuple[list[subprocess.CompletedProcess[str]], Path]:
    repo = Path(__file__).parents[2]
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "primary",
    }))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    results = [
        _run(repo, config, "stop-capture", {
            "hook_event_name": "Stop", "session_id": "provenance-session",
            "transcript_path": str(transcript), "cwd": str(tmp_path),
            "stop_hook_active": False,
        })
        for _ in range(runs)
    ]
    return results, data


def test_stop_capture_records_human_origin_feedback(tmp_path):
    (result,), data = _stop_capture_over_transcript(tmp_path, [
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


def test_stop_capture_records_prompt_source_feedback_without_origin(tmp_path):
    # The CLI versions that record no origin still stamp submitted prompts.
    (result,), data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "I omitted one failed source."}},
        {
            "type": "user", "promptSource": "typed", "isSidechain": False,
            "message": {"content": "No, report every failed source explicitly."},
        },
    ])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "queued"
    assert len(sorted((data / "test" / "spool" / "primary").glob("*.json"))) == 1


@pytest.mark.parametrize(("entry", "basis"), [
    # Declared machine provenance decides alone, even on feedback-shaped text.
    ({"type": "user", "origin": {"kind": "task-notification"},
      "message": {"content": TASK_NOTIFICATION}}, "origin"),
    # Structural tells drop an entry that otherwise looks like a prompt.
    ({"type": "user", "promptSource": "typed", "isSidechain": True,
      "message": {"content": "No, that heading is wrong."}}, "structure"),
    ({"type": "user", "promptSource": "typed", "toolUseResult": {"stdout": "ok"},
      "message": {"content": "No, that heading is wrong."}}, "structure"),
    # No origin and no promptSource: not a submitted prompt at all. The marker
    # blacklist survives only to name why recognisable host text was dropped.
    ({"type": "user", "message": {"content": SKILL_BODY}}, "marker"),
    ({"type": "user", "message": {"content": INTERRUPTION_NOTICE}}, "structure"),
])
def test_stop_capture_skips_synthetic_transcript_entries(tmp_path, entry, basis):
    (result,), data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "Working."}}, entry,
    ])
    # The skip path must stay exit 0: exit 2 from a Stop hook blocks the host.
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "hook_schema_version": "1.0.0",
        "provenance_basis": basis,
        "reason": "synthetic_transcript_entry",
        "status": "skipped",
    }
    assert not list((data / "test" / "spool").rglob("*.json"))


def test_stop_capture_skips_a_tool_result_only_transcript_without_blocking_stop(tmp_path):
    # Blank-text user entries once bypassed the gate and reached a raise that
    # exits 2, which blocks the host session's stop.
    (result,), data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "Running the suite."}},
        {"type": "user", "toolUseResult": {"stdout": "419 passed"},
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "toolu_1", "content": "419 passed"},
         ]}},
    ])
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["status"] == "skipped"
    assert not list((data / "test" / "spool").rglob("*.json"))


def test_stop_capture_reaches_past_synthetic_entries_to_the_operator_turn(tmp_path):
    (result,), data = _stop_capture_over_transcript(tmp_path, [
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


def test_repeated_stop_capture_over_an_unchanged_transcript_captures_once(tmp_path):
    # Stop fires on every assistant turn, so the same operator utterance is
    # re-read until the operator speaks again.
    results, data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "I omitted one failed source."}},
        {
            "type": "user", "origin": {"kind": "human"}, "uuid": "entry-uuid-1",
            "message": {"content": "No, report every failed source explicitly."},
        },
    ], runs=3)
    assert [result.returncode for result in results] == [0, 0, 0]
    bodies = [json.loads(result.stdout) for result in results]
    assert bodies[0]["status"] == "queued"
    for body in bodies[1:]:
        assert body == {
            "hook_schema_version": "1.0.0",
            "reason": "operator_turn_already_captured",
            "status": "skipped",
        }
    assert len(sorted((data / "test" / "spool" / "primary").glob("*.json"))) == 1


def test_stop_capture_captures_the_next_operator_turn_after_an_idempotent_skip(tmp_path):
    first_turn = {
        "type": "user", "origin": {"kind": "human"}, "uuid": "entry-uuid-1",
        "message": {"content": "No, report every failed source explicitly."},
    }
    results, data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "I omitted one failed source."}},
        first_turn,
    ], runs=2)
    assert json.loads(results[0].stdout)["status"] == "queued"
    assert json.loads(results[1].stdout)["reason"] == "operator_turn_already_captured"
    results, data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "I omitted one failed source."}},
        first_turn,
        {"type": "assistant", "message": {"content": "Reporting all sources."}},
        {
            "type": "user", "origin": {"kind": "human"}, "uuid": "entry-uuid-2",
            "message": {"content": "No, keep the source labels on every row."},
        },
    ])
    assert json.loads(results[0].stdout)["status"] == "queued"
    spooled = sorted((data / "test" / "spool" / "primary").glob("*.json"))
    assert len(spooled) == 2


def test_a_failed_capture_releases_the_turn_mark_so_the_retry_captures(tmp_path):
    # The once-capture mark is claimed before the envelope is written, so a
    # write that fails must give the claim back. A mark left over a failed
    # capture makes the healed retry skip the turn forever.
    repo = Path(__file__).parents[2]
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "primary",
    }))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "user", "origin": {"kind": "human"}, "uuid": "entry-uuid-1",
        "message": {"content": "No, report every failed source explicitly."},
    }) + "\n")
    event = {
        "hook_event_name": "Stop", "session_id": "unwind-session",
        "transcript_path": str(transcript), "cwd": str(tmp_path),
        "stop_hook_active": False,
    }
    # Block the spool write the way a real disk fault would: the node's spool
    # directory cannot be created because its path is taken by a file.
    blocked = data / "test" / "spool" / "primary"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("not a directory")

    failed = _run(repo, config, "stop-capture", event)
    assert failed.returncode == 2
    assert json.loads(failed.stdout)["status"] == "error"
    marks = data / "test" / "runtime" / "captured-turns"
    assert not list(marks.rglob("*.json")), "a failed capture must not keep the claim"

    blocked.unlink()
    healed = _run(repo, config, "stop-capture", event)
    assert healed.returncode == 0, healed.stderr
    assert json.loads(healed.stdout)["status"] == "queued"
    spooled = sorted((data / "test" / "spool" / "primary").glob("*.json"))
    assert len(spooled) == 1
    assert "report every failed source" in spooled[0].read_text()
    assert len(list(marks.rglob("*.json"))) == 1


def test_stop_capture_hears_a_correction_behind_a_prepended_host_reminder(tmp_path):
    # The host prepends its own blocks to a genuinely submitted prompt, which
    # once pushed the operator's first sentence out of reach of the anchored
    # correction rule and dropped the turn as non-feedback.
    (result,), data = _stop_capture_over_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "I omitted one failed source."}},
        {
            "type": "user", "origin": {"kind": "human"},
            "message": {"content": f"{SYSTEM_REMINDER}\n\nNo, keep the failed source in the summary."},
        },
    ])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "queued"
    spooled = sorted((data / "test" / "spool" / "primary").glob("*.json"))
    assert len(spooled) == 1
    assert "keep the failed source" in spooled[0].read_text()


def test_hook_rejects_wrong_event_contract(tmp_path):
    repo = Path(__file__).parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "operator_slug": "test"}))
    result = _run(repo, config, "stop-capture", {
        "hook_schema_version": "9.0.0", "hook_event_name": "SessionStart",
    })
    assert result.returncode == 2
    assert json.loads(result.stdout)["error_type"] == "ValidationError"
