"""A short line says the rule and how far to trust it, and the rest is one lookup away."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import imprint.cli as cli
from imprint.config import DEFAULTS, SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START, parse_config
from imprint.errors import ValidationError
from imprint.store import ImprintStore


def write_config(tmp_path: Path, **values) -> tuple[Path, Path]:
    data = tmp_path / "data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(data), "operator_slug": "test",
        "node_id": "primary", "compiler": True, "context_budget_bytes": 4096, **values,
    }))
    return config, data / "test"


def hook(config: Path, action: str, event: dict, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    assert cli.main(["--config", str(config), "hook", action]) == 0
    return json.loads(capsys.readouterr().out)


def seed(config: Path, monkeypatch, capsys, words="No, report the failed source because omissions change it.") -> None:
    receipt = hook(config, "stop-capture", {"session_id": "seed", "operator_text": words}, monkeypatch, capsys)
    assert receipt["canonical_status"] == "compiled", receipt


def history(config: Path, node_id: str, capsys) -> dict:
    assert cli.main(["--config", str(config), "history", node_id]) == 0
    return json.loads(capsys.readouterr().out)["history"]


def delivered(body: dict) -> list[dict]:
    return [json.loads(line) for line in body["hookSpecificOutput"]["additionalContext"].splitlines()]


# -- configuration -------------------------------------------------------------------

@pytest.mark.parametrize("key", [SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START])
def test_each_switch_is_off_by_default_and_dotted_at_the_top_level(key):
    assert DEFAULTS[key] is False
    assert "." in key
    assert parse_config({})[key] is False


def test_the_switches_are_separate():
    config = parse_config({SHORT_LINES_PROMPT: True})
    assert (config[SHORT_LINES_PROMPT], config[SHORT_LINES_SESSION_START]) == (True, False)
    config = parse_config({SHORT_LINES_SESSION_START: True})
    assert (config[SHORT_LINES_PROMPT], config[SHORT_LINES_SESSION_START]) == (False, True)


@pytest.mark.parametrize("key", [SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START])
@pytest.mark.parametrize("value", [1, 0, "true", None, []])
def test_a_switch_must_be_a_boolean(key, value):
    with pytest.raises(ValidationError, match="must be a boolean"):
        parse_config({key: value})


def test_no_key_was_added_to_the_closed_selector_object():
    assert "short_lines" not in json.dumps(DEFAULTS["selector"])


# -- imprint history -----------------------------------------------------------------

def test_history_returns_the_line_a_whole_delivery_carries(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    (line,) = delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))
    found = history(config, line["record_id"], capsys)["whole_line"]
    assert found == line
    assert found["evidence_ids"] and found["case_ids"] and found["case_summaries"] and found["source_receipt_ids"]
    assert found["ontology"]["disclosure"] == "operator_captured"
    assert found["ontology"]["path"] == ["judgment", "Verdict"]


def proposal(store: ImprintStore) -> str:
    return store.append_derived_node(
        node_type="Principle", payload={"statement": "Report failures explicitly."},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[item["node_id"] for item in store.current_nodes(["Evidence"])],
        operator_id=store.current_nodes(["Evidence"])[0]["operator_id"],
        valid_from="2026-07-14T18:00:00Z", proposed_by="contract-test",
    )


def test_history_gives_the_labels_no_version_holds(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    node_id = proposal(ImprintStore(root / "imprint.db"))
    found = history(config, node_id, capsys)
    assert "model_inference_not_operator_authority" not in json.dumps(found["versions"])
    assert found["whole_line"]["ontology"] == {
        "confidence": None, "disclosure": "model_inference_not_operator_authority",
        "partition": "judgment", "path": ["judgment", "Principle"], "type": "Principle",
    }
    assert found["whole_line"]["authority"] == "inferred"


def test_history_keeps_what_it_always_returned(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    (line,) = delivered(hook(config, "session-start", {"session_id": "s"}, monkeypatch, capsys))
    found = history(config, line["record_id"], capsys)
    assert set(found) == {"node_id", "versions", "dispositions", "whole_line"}
    assert found["versions"][-1]["payload"]["raw_operator_text"] == line["text"]


def test_a_node_retrieval_never_delivers_has_no_line(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    (evidence,) = ImprintStore(root / "imprint.db").current_nodes(["Evidence"])
    assert history(config, evidence["node_id"], capsys)["whole_line"] is None


def test_a_record_closed_since_delivery_says_so_instead_of_a_line(tmp_path, monkeypatch, capsys):
    config, root = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    node_id = proposal(ImprintStore(root / "imprint.db"))
    assert cli.main(["--config", str(config), "review", "reject", node_id, "--by", "operator", "--reason", "too broad"]) == 0
    capsys.readouterr()
    found = history(config, node_id, capsys)
    assert found["dispositions"][0]["event_type"] == "rejected"
    assert found["versions"][-1]["payload"]["statement"] == "Report failures explicitly."
    assert found["whole_line"] is None


def test_an_unknown_record_still_fails(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    seed(config, monkeypatch, capsys)
    assert cli.main(["--config", str(config), "history", "urn:imprint:verdict:unknown"]) != 0
    capsys.readouterr()
