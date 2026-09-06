"""Transport provenance must prevent machine prompts from entering operator canon."""
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from imprint.capture.provenance import classify_entry_provenance
from imprint.capture.transcript import parse_native_stop_transcript
from imprint.cli import _parse_large_native_transcript

REPO = Path(__file__).resolve().parents[2]
FEEDBACK = "No, explicitly report failed sources because omission changes the decision."

@pytest.mark.parametrize("origin,source,expected", [
    (None, "sdk", False), (None, "future-source", False),
    (None, "typed", True), (None, "queued", True),
    (None, "suggestion_accepted", True), (None, "system", False), ({"kind": "human"}, "sdk", True),
    ({"kind": "automation"}, "typed", False),
])
def test_source_is_authorship_not_just_prompt_submission(origin, source, expected):
    entry = {"origin": origin, "promptSource": source}
    assert classify_entry_provenance(entry, FEEDBACK).is_operator is expected

@pytest.mark.parametrize("parser", [parse_native_stop_transcript, _parse_large_native_transcript])
def test_sdk_null_origin_is_reported_unverified(parser, tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"type": "user", "origin": None, "promptSource": "sdk",
                               "message": {"content": FEEDBACK}}) + "\n")
    result = parser(str(path))
    assert result["skip_reason"] == "unverified_provenance"
    assert result["operator_text"] is None

@pytest.mark.parametrize("transport,origin,source,expected", [
    ("automation", {"kind": "human"}, "typed", "machine_origin"),
    ("", None, "sdk", "unverified_provenance"),
    ("", None, "typed", "queued"),
    ("", None, "queued", "queued"),
    ("", None, "suggestion_accepted", "queued"),
    ("", {"kind": "human"}, "sdk", "queued"),
])
def test_isolated_capture_writes_only_for_verified_human(tmp_path, transport, origin, source, expected):
    config = tmp_path / "config.json"
    data = tmp_path / "data"
    config.write_text(json.dumps({"data_root": str(data), "operator_slug": "test", "node_id": "test"}))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "user", "origin": origin, "promptSource": source,
                                     "message": {"content": FEEDBACK + " A human may quote an automation prompt."}}) + "\n")
    event = {"session_id": "synthetic", "transcript_path": str(transcript)}
    if transport:
        event["operator_text"] = FEEDBACK  # Explicit payload cannot override machine transport.
    env = dict(os.environ, IMPRINT_CONFIG=str(config), IMPRINT_CAPTURE_ORIGIN=transport)
    run = subprocess.run([sys.executable, "-m", "imprint.cli", "hook", "stop-capture"],
                         input=json.dumps(event), text=True, capture_output=True, env=env, cwd=REPO)
    assert run.returncode == 0, run.stderr
    body = json.loads(run.stdout)
    assert body.get("reason", body["status"]) == expected
    if expected != "queued":
        assert not list(data.rglob("*.db"))
        assert not list(data.rglob("*.jsonl"))


def test_bridge_passes_machine_origin_to_cli():
    spec = importlib.util.spec_from_file_location("bridge_origin", REPO / "hooks/_bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    with patch.dict(os.environ, {"IMPRINT_CAPTURE_ORIGIN": "automation"}), patch.object(bridge.subprocess, "Popen") as popen:
        popen.return_value.communicate.return_value = ("{}", "")
        popen.return_value.returncode = 0
        bridge._graceful_run(["synthetic-cli"], "{}")
        assert popen.call_args.kwargs["env"]["IMPRINT_CAPTURE_ORIGIN"] == "automation"
