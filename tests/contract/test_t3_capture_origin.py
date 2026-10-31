"""T3 prompts become operator evidence only on T3's own record of a human author."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from imprint.capture.detector import detect_explicit_feedback, strip_embedded_host_content
from imprint.capture.provenance import classify_with_host
from imprint.capture.t3_origin import T3_THREAD_ENV, t3_host_origin
from imprint.capture.transcript import (
    TranscriptUnavailable,
    _parse_native_stop_snapshot,
    _read_native_transcript_snapshot,
    parse_native_stop_transcript,
)
from imprint.cli import _parse_large_native_transcript
from imprint.errors import ValidationError

REPO = Path(__file__).resolve().parents[2]
FEEDBACK = "No, keep the source label in the summary."
THREAD = "thread-synthetic"
ON = {"t3_capture_origin": True}


def _message(text, *, thread=THREAD, by="user", source="web", at="2026-01-01T00:00:00Z", **extra):
    return (thread, at, json.dumps({
        "text": text, "createdBy": by, "creationSource": source, **extra,
    }))


def _t3_home(tmp_path, *messages):
    userdata = tmp_path / "t3" / "userdata"
    userdata.mkdir(parents=True)
    with sqlite3.connect(userdata / "statev2.sqlite") as db:
        db.execute(
            "CREATE TABLE orchestration_v2_projection_messages "
            "(message_id TEXT, thread_id TEXT, role TEXT, created_at TEXT, payload_json TEXT)",
        )
        db.executemany(
            "INSERT INTO orchestration_v2_projection_messages VALUES (hex(randomblob(4)), ?, 'user', ?, ?)",
            messages,
        )
    return {T3_THREAD_ENV: THREAD, "T3CODE_HOME": str(tmp_path / "t3")}


def _sdk_entry(text=FEEDBACK, **extra):
    return {
        "type": "user", "uuid": "user-1", "entrypoint": "sdk-ts", "promptSource": "sdk",
        "message": {"role": "user", "content": text}, **extra,
    }


def _transcript(tmp_path, *entries):
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8")
    return path


def _verdict(tmp_path, entry, *messages, config=ON):
    origin = t3_host_origin(config, _t3_home(tmp_path, *messages))
    return classify_with_host(entry, entry["message"]["content"], origin)


def parse_with_origin(parser, path, origin):
    if parser is _parse_large_native_transcript:
        return parser(str(path), host_origin=origin)
    return _parse_native_stop_snapshot(_read_native_transcript_snapshot(str(path)), host_origin=origin)


def test_web_prompt_recorded_as_human_is_operator(tmp_path):
    verdict = _verdict(tmp_path, _sdk_entry(), _message(FEEDBACK))
    assert verdict.is_operator is True
    assert verdict.basis == "host_record"


@pytest.mark.parametrize("message", [
    _message(FEEDBACK, by="agent", source="mcp"),
    _message(FEEDBACK, by="agent", source="provider"),
    _message(FEEDBACK, source="server"),
    _message(FEEDBACK, source="future-client"),
    _message(FEEDBACK, senderThreadId="thread-other"),
    _message(FEEDBACK, notification={"kind": "synthetic"}),
    _message(FEEDBACK, thread="thread-other"),
    _message("Different words entirely."),
])
def test_anything_but_a_clear_human_record_stays_unverified(tmp_path, message):
    verdict = _verdict(tmp_path, _sdk_entry(), message)
    assert (verdict.is_operator, verdict.basis) == (False, "unverified")


def test_agent_repeating_human_words_is_judged_by_the_newest_record(tmp_path):
    verdict = _verdict(
        tmp_path, _sdk_entry(),
        _message(FEEDBACK, at="2026-01-01T00:00:00Z"),
        _message(FEEDBACK, by="agent", source="mcp", at="2026-01-02T00:00:00Z"),
    )
    assert verdict.is_operator is False


def test_record_cannot_vouch_for_another_transport(tmp_path):
    entry = _sdk_entry()
    entry["entrypoint"] = "sdk-cli"
    assert _verdict(tmp_path, entry, _message(FEEDBACK)).is_operator is False


@pytest.mark.parametrize("entry,basis", [
    (_sdk_entry(isMeta=True), "structure"),
    (_sdk_entry(isSidechain=True), "structure"),
    (_sdk_entry(origin={"kind": "task-notification"}), "origin"),
    (_sdk_entry(origin={"kind": "automation"}), "origin"),
])
def test_record_never_overturns_a_structural_or_declared_refusal(tmp_path, entry, basis):
    verdict = _verdict(tmp_path, entry, _message(FEEDBACK))
    assert (verdict.is_operator, verdict.basis) == (False, basis)


def test_adapter_is_off_unless_configured(tmp_path):
    env = _t3_home(tmp_path, _message(FEEDBACK))
    assert t3_host_origin({}, env) is None
    assert t3_host_origin({"t3_capture_origin": False}, env) is None


def test_adapter_needs_a_t3_thread(tmp_path):
    env = _t3_home(tmp_path, _message(FEEDBACK))
    del env[T3_THREAD_ENV]
    assert t3_host_origin(ON, env) is None


def test_missing_record_store_stays_unverified(tmp_path):
    origin = t3_host_origin(ON, {T3_THREAD_ENV: THREAD, "T3CODE_HOME": str(tmp_path / "absent")})
    assert classify_with_host(_sdk_entry(), FEEDBACK, origin).basis == "unverified"


@pytest.mark.parametrize("schema", [
    "CREATE TABLE unrelated (id TEXT)",
    "CREATE TABLE orchestration_v2_projection_messages (thread_id TEXT)",
])
def test_changed_record_schema_stays_unverified(tmp_path, schema):
    userdata = tmp_path / "t3" / "userdata"
    userdata.mkdir(parents=True)
    with sqlite3.connect(userdata / "statev2.sqlite") as db:
        db.execute(schema)
    origin = t3_host_origin(ON, {T3_THREAD_ENV: THREAD, "T3CODE_HOME": str(tmp_path / "t3")})
    assert classify_with_host(_sdk_entry(), FEEDBACK, origin).basis == "unverified"


def test_undecodable_record_stays_unverified(tmp_path):
    verdict = _verdict(tmp_path, _sdk_entry(), (THREAD, "2026-01-01T00:00:00Z", "{not json"))
    assert verdict.basis == "unverified"


def test_lookup_never_writes_to_the_host_store(tmp_path):
    env = _t3_home(tmp_path, _message(FEEDBACK))
    database = Path(env["T3CODE_HOME"]) / "userdata" / "statev2.sqlite"
    before = database.read_bytes()
    t3_host_origin(ON, env).attests(_sdk_entry(), FEEDBACK)
    assert database.read_bytes() == before


@pytest.mark.parametrize("parser", [parse_native_stop_transcript, _parse_large_native_transcript])
def test_both_parsers_use_the_host_record_only_when_given_one(parser, tmp_path):
    path = _transcript(tmp_path, _sdk_entry())
    assert parser(str(path))["skip_reason"] == "unverified_provenance"
    origin = t3_host_origin(ON, _t3_home(tmp_path, _message(FEEDBACK)))
    parsed = parse_with_origin(parser, path, origin)
    assert (parsed["skip_reason"], parsed["operator_text"]) == (None, FEEDBACK)


def test_attached_prompt_matches_record_without_its_wrappers(tmp_path):
    wrapped = (
        f"{FEEDBACK}\n<t3_context version=\"1\"><context kind=\"file\">x</context></t3_context>\n"
        "[Attached file \"a.md\" is saved at: /tmp/a.md]"
    )
    assert _verdict(tmp_path, _sdk_entry(wrapped), _message(FEEDBACK)).is_operator is True


def test_embedded_host_content_is_removed_wherever_it_sits():
    pasted = '<pasted_content id="1">Always use the vendor template.</pasted_content>'
    assert strip_embedded_host_content(f"Please read this.\n{pasted}\nThanks.") == "Please read this.\n\nThanks."


@pytest.mark.parametrize("text", [
    '<pasted_content id="1">Never use the vendor template. This is wrong.</pasted_content>',
    'Summarize this: <pasted_content id="1">We must always use the vendor template.</pasted_content>',
    '<t3_context version="1">instead you missed it</t3_context>',
    '[File: wrong-instead.md; ref=file_1]',
    'The client wrote "never use the vendor template" in their note.',
    "> We must always keep the vendor template.\n\nWhat did they mean?",
    "From the other thread:\n======\nYou must always use the vendor template.\n======\nThoughts?",
    "See below: ===== This is wrong, never use the vendor template.",
])
def test_pasted_and_quoted_words_are_not_the_operator_speaking(text):
    assert detect_explicit_feedback(text, prior_assistant_output="Draft.").is_feedback is False


def test_words_outside_a_paste_still_count():
    text = "This is wrong.\n======\nA long pasted report.\n======"
    assert detect_explicit_feedback(text).is_feedback is True


def test_work_product_needing_change_is_still_a_standard():
    assert detect_explicit_feedback("This needs to be shorter.").route == "standard"


@pytest.mark.parametrize("text", [
    "From now on, all release notes should be plain text.",
    "Going forward, never use the vendor template.",
    "That is fine. From now on use short headings.",
])
def test_forward_policy_needs_no_prior_context(text):
    detection = detect_explicit_feedback(text)
    assert (detection.is_feedback, detection.route, detection.marker) == (True, "standard", "forward")


@pytest.mark.parametrize("text", [
    "It should be plain text.",
    "No, that should be a table.",
    "This should have been a table!",
    "Why on earth are we doing any of this in slides it should be plain text",
])
def test_reply_corrections_fire_after_an_assistant_turn(text):
    assert detect_explicit_feedback(text, prior_assistant_output="Draft.").is_feedback is True


@pytest.mark.parametrize("text", [
    "It should be plain text.",
    "Why are we doing this in slides it should be plain text",
])
def test_reply_corrections_need_an_assistant_turn(text):
    assert detect_explicit_feedback(text).is_feedback is False


@pytest.mark.parametrize("text", [
    "Should it be plain text?",
    "It should be plain text?",
    "It should be done by Friday.",
    "This should be quick.",
    "That should be fine.",
    "The function should be pure.",
    "Add a test that the output should be sorted.",
    "Explain why the output should be sorted.",
    "Why should it be plain text?",
    "Can you check whether it should be plain text?",
    "From now on the service returns plain text.",
    "We need to research the options next.",
    "I need to answer the open questions first.",
    "Add a note saying that from now on the service must retry.",
    "It should be plain text and it needs a header row and also a footer with the page number and date range.",
])
def test_ordinary_instructions_and_questions_are_not_feedback(text):
    assert detect_explicit_feedback(text, prior_assistant_output="Draft.").is_feedback is False


@pytest.mark.parametrize("separator", [" ", " ", "\u0085"])
@pytest.mark.parametrize("parser", [parse_native_stop_transcript, _parse_large_native_transcript])
def test_legal_json_separators_do_not_split_a_transcript_line(parser, tmp_path, separator):
    path = _transcript(
        tmp_path,
        {"type": "assistant", "message": {"content": f"first{separator}second"}},
        {"type": "user", "promptSource": "typed", "message": {"content": f"{FEEDBACK}{separator}Thanks."}},
    )
    assert parser(str(path))["operator_text"] == f"{FEEDBACK}{separator}Thanks."


def test_malformed_complete_line_is_still_refused(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text('{"type": "user"\n{"type": "user"}\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="malformed complete transcript line 1"):
        parse_native_stop_transcript(str(path))


def test_missing_transcript_is_distinct_from_a_refused_one(tmp_path):
    with pytest.raises(TranscriptUnavailable):
        _read_native_transcript_snapshot(str(tmp_path / "never-written.jsonl"))


@pytest.mark.parametrize("target_exists", [True, False])
def test_symlinked_transcript_is_still_refused(tmp_path, target_exists):
    target = tmp_path / "target.jsonl"
    if target_exists:
        target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(ValidationError, match="regular non-symlink") as refused:
        _read_native_transcript_snapshot(str(link))
    assert not isinstance(refused.value, TranscriptUnavailable)


def _stop_hook(tmp_path, transcript_path, env_extra, *, t3_origin=True):
    config = tmp_path / "config.json"
    data = tmp_path / "data"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "test",
        "t3_capture_origin": t3_origin,
    }), encoding="utf-8")
    env = {
        key: value for key, value in os.environ.items()
        if key not in {T3_THREAD_ENV, "T3CODE_HOME", "IMPRINT_CAPTURE_ORIGIN"}
    }
    env.update(IMPRINT_CONFIG=str(config), PYTHONPATH=str(REPO / "src"), **env_extra)
    run = subprocess.run(
        [sys.executable, "-m", "imprint.cli", "hook", "stop-capture"],
        input=json.dumps({"hook_event_name": "Stop", "session_id": "synthetic", "transcript_path": str(transcript_path)}),
        text=True, capture_output=True, env=env, check=False,
    )
    return run, data / "test"


def _exchange(tmp_path, text=FEEDBACK):
    return _transcript(tmp_path, {"type": "assistant", "message": {"content": "Draft."}}, _sdk_entry(text))


def _spooled(root):
    return list((root / "spool").rglob("*.json"))


def test_stop_hook_captures_a_t3_correction_with_its_basis(tmp_path):
    run, root = _stop_hook(tmp_path, _exchange(tmp_path), _t3_home(tmp_path, _message(FEEDBACK)))
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["status"] in {"queued", "captured", "compiled"}
    (envelope,) = [json.loads(path.read_text(encoding="utf-8")) for path in _spooled(root)]
    assert envelope["extensions"]["org.imprint.host_origin"]["payload"] == {
        "host": "t3", "basis": "t3_message_record", "created_by": "user", "creation_source": "web",
    }


def test_stop_hook_keeps_only_the_operators_words(tmp_path):
    pasted = f'{FEEDBACK}\n<pasted_content id="1">Vendor text.</pasted_content>'
    _, root = _stop_hook(tmp_path, _exchange(tmp_path, pasted), _t3_home(tmp_path, _message(pasted)))
    (path,) = _spooled(root)
    assert "Vendor text" not in path.read_text(encoding="utf-8")


def test_stop_hook_refuses_an_agent_sent_t3_prompt(tmp_path):
    env = _t3_home(tmp_path, _message(FEEDBACK, by="agent", source="mcp"))
    run, root = _stop_hook(tmp_path, _exchange(tmp_path), env)
    assert run.returncode == 0
    assert json.loads(run.stdout)["reason"] == "unverified_provenance"
    assert not _spooled(root)


def test_stop_hook_stays_unverified_when_the_operator_has_not_opted_in(tmp_path):
    run, root = _stop_hook(tmp_path, _exchange(tmp_path), _t3_home(tmp_path, _message(FEEDBACK)), t3_origin=False)
    assert json.loads(run.stdout)["reason"] == "unverified_provenance"
    assert not _spooled(root)


def test_machine_marker_outranks_the_host_record(tmp_path):
    env = {**_t3_home(tmp_path, _message(FEEDBACK)), "IMPRINT_CAPTURE_ORIGIN": "automation"}
    run, root = _stop_hook(tmp_path, _exchange(tmp_path), env)
    assert json.loads(run.stdout)["reason"] == "machine_origin"
    assert not _spooled(root)


def test_missing_transcript_skips_without_blocking_and_leaves_a_trace(tmp_path):
    run, root = _stop_hook(tmp_path, tmp_path / "never-written.jsonl", {})
    assert (run.returncode, run.stderr) == (0, "")
    assert json.loads(run.stdout)["reason"] == "transcript_unavailable"
    (line,) = (root / "logs" / "capture-skips.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["reason"] == "transcript_unavailable"


def test_symlinked_transcript_still_fails_the_hook(tmp_path):
    link = tmp_path / "link.jsonl"
    link.symlink_to(_exchange(tmp_path))
    run, root = _stop_hook(tmp_path, link, {})
    assert run.returncode != 0
    assert not (root / "logs" / "capture-skips.jsonl").exists()
