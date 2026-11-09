"""``imprint carrier codex``: off unless configured, silent, and always exit 0."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
QUEUE_KEY = "org.imprint.client_carrier.queue"
CLIENTS_KEY = "org.imprint.client_carrier.clients"

CLIENTS = {
    "rooms": [{
        "id": "northwind", "kind": "client",
        "name_patterns": [{"re": r"\bMara Quill\b", "signal": "person_full_name", "strength": "strong"}],
        "people": [{"name": "Mara Quill"}],
        "thread_title_prefixes": ["NW ·"],
        "folders": {},
    }],
}


@pytest.fixture
def world(tmp_path):
    codex = tmp_path / "codex-home"
    codex.mkdir()
    connection = sqlite3.connect(codex / "state_5.sqlite")
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, source TEXT, thread_source TEXT, "
        "agent_nickname TEXT, agent_role TEXT, agent_path TEXT, attached_thread_parent_id TEXT)"
    )
    connection.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)")
    connection.execute("INSERT INTO threads VALUES ('t-1', 'NW · pages', 'vscode', 'user', NULL, NULL, NULL, NULL)")
    connection.commit()
    connection.close()
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps(CLIENTS))
    work = tmp_path / "work"
    work.mkdir()
    data = tmp_path / "data"
    queue = tmp_path / "queues" / "carrier.jsonl"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test", "node_id": "test",
        QUEUE_KEY: str(queue), CLIENTS_KEY: str(clients),
    }))
    event = {
        "hook_event_name": "UserPromptSubmit", "session_id": "t-1", "turn_id": "turn-1",
        "cwd": str(work), "prompt": "Mara Quill wrote:\nthe header is too big",
    }
    return {"codex": codex, "config": config, "queue": queue, "data": data, "event": event, "tmp": tmp_path}


def run(world, payload, *, config=None, args=("carrier", "codex")):
    env = dict(os.environ, CODEX_HOME=str(world["codex"]), IMPRINT_CONFIG=str(config or world["config"]))
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return subprocess.run(
        [sys.executable, "-m", "imprint.cli", *args], input=data, capture_output=True, env=env, cwd=REPO,
    )


def test_a_configured_step_appends_one_row_and_prints_nothing(world):
    result = run(world, world["event"])
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    (line,) = world["queue"].read_text().splitlines()
    row = json.loads(line)
    assert (row["client"]["status"], row["client"]["guess"], row["message_id"]) == ("client", "northwind", "turn-1")
    assert "header" not in line
    # The store is never opened: nothing is written under the data root.
    assert not world["data"].exists() or not any(world["data"].rglob("*"))


def test_with_no_queue_configured_it_does_nothing(world, tmp_path):
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({"data_root": str(world["data"]), "operator_slug": "test", "node_id": "test"}))
    result = run(world, world["event"], config=plain)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert not world["queue"].exists()
    result = run(world, world["event"], config=tmp_path / "absent.json")
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert not world["queue"].exists()


@pytest.mark.parametrize("payload", [b"{not json", b"", b"[1, 2]", b"\xff\xfe", b" " * (9 * 1024 * 1024)])
def test_bad_or_oversized_input_exits_0_silently(world, payload):
    result = run(world, payload)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert not world["queue"].exists()


def test_a_corrupt_or_invalid_config_exits_0_silently(world, tmp_path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{")
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({QUEUE_KEY: "relative/queue.jsonl"}))
    for config in (corrupt, invalid):
        result = run(world, world["event"], config=config)
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert not world["queue"].exists()


def test_a_subagent_prompt_makes_no_row(world):
    result = run(world, {**world["event"], "agent_id": "sub-1", "agent_type": "worker"})
    assert (result.returncode, result.stdout) == (0, b"")
    assert not world["queue"].exists()


def test_the_global_config_flag_is_honored(world, tmp_path):
    env_config = tmp_path / "plain.json"
    env_config.write_text("{}")
    result = run(world, world["event"], config=env_config, args=("--config", str(world["config"]), "carrier", "codex"))
    assert (result.returncode, result.stdout) == (0, b"")
    assert world["queue"].read_text().count("\n") == 1
