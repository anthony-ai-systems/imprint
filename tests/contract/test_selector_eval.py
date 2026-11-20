"""The offline selector evaluation: real prefilter, pick policy and renderer, saved
scores in place of Jev, and no network, config or store. Every case is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import imprint.cli as cli
from imprint.decide.evaluate import evaluate, load_cases, load_scores

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "selector_eval"


# -- the offline selector evaluation -------------------------------------------------

def run_eval(monkeypatch, capsys, *args) -> tuple[int, dict]:
    code = cli.main(["selector-eval", "--cases", str(FIXTURE / "cases.json"), "--scores",
                     str(FIXTURE / "scores.json"), *args])
    return code, json.loads(capsys.readouterr().out)


def test_the_evaluation_reports_recall_precision_and_unrelated_pickups(monkeypatch, capsys):
    code, report = run_eval(monkeypatch, capsys)
    assert code == 0
    assert {key: value for key, value in report.items() if key != "per_case"} == {
        "cases": 4, "candidates": 16, "gold": 7,
        "settings": {"budget_bytes": 32768, "lines": "whole", "score_floor": 0.55, "shortlist": 150, "top": 20},
        "shortlisted_gold": 7, "recall_at_shortlist": 1.0, "unscored": 1, "picked": 8,
        "rendered": 8, "rendered_gold": 6, "precision_rendered": 0.75, "unrelated_pickups": 2,
    }
    by_case = {case["id"]: case for case in report["per_case"]}
    assert (by_case["release-notes"]["rendered_gold"], by_case["release-notes"]["unrelated_pickups"]) == (2, 1)
    assert (by_case["thanks-only"]["gold"], by_case["thanks-only"]["unrelated_pickups"]) == (0, 1)
    assert by_case["checkout-tests"]["unscored"] == 1


def test_a_short_shortlist_loses_recall(monkeypatch, capsys):
    _, report = run_eval(monkeypatch, capsys, "--shortlist", "2")
    assert (report["shortlisted_gold"], report["recall_at_shortlist"]) == (5, 0.7143)
    assert (report["rendered"], report["rendered_gold"], report["precision_rendered"]) == (5, 4, 0.8)


def test_the_floor_and_top_are_todays_pick_policy(monkeypatch, capsys):
    _, high = run_eval(monkeypatch, capsys, "--score-floor", "0.85")
    assert (high["picked"], high["rendered_gold"], high["unrelated_pickups"]) == (3, 3, 0)
    _, one = run_eval(monkeypatch, capsys, "--top", "1")
    assert one["picked"] == 4 and one["precision_rendered"] == 0.75


def test_the_budget_bounds_what_is_rendered():
    case = {
        "id": "crowded", "turn": "synthetic ledger rounding review",
        "candidates": [
            {"id": f"j{n}", "statement": f"Synthetic ledger rounding rule number {n}. " + "Padding words here. " * 40}
            for n in range(12)
        ],
        "gold": ["j0", "j1"],
    }
    scores = {"crowded": {f"j{n}": 0.99 - n / 100 for n in range(12)}}
    wide = evaluate([case], scores)
    tight = evaluate([case], scores, budget=4096)
    assert wide["rendered"] == 12 and wide["per_case"][0]["rendered_compact"] == 0
    assert tight["rendered"] < 12 or tight["per_case"][0]["rendered_compact"] > 0
    assert tight["per_case"][0]["rendered_bytes"] <= 4096
    assert tight["precision_rendered"] == round(2 / tight["rendered"], 4)
    assert evaluate([case], scores, short_lines=True)["settings"]["lines"] == "short"


def test_the_evaluation_output_holds_ids_and_counts_only(monkeypatch, capsys):
    _, report = run_eval(monkeypatch, capsys)
    text = json.dumps(report)
    for case in load_cases(FIXTURE / "cases.json"):
        assert case["turn"] not in text
        for candidate in case["candidates"]:
            assert candidate["statement"] not in text
    assert "lantern" not in text and "garden planner" not in text


def test_the_evaluation_reads_no_config_and_no_store(tmp_path, monkeypatch, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setenv("IMPRINT_CONFIG", str(broken))
    code, report = run_eval(monkeypatch, capsys)
    assert code == 0 and report["cases"] == 4
    assert not (tmp_path / "imprint-isolated-data").exists()


@pytest.mark.parametrize("cases, scores, message", [
    ({"cases": []}, {"scores": {}}, "non-empty cases"),
    ({"cases": [{"id": "Bad Id", "turn": "t", "candidates": [{"id": "a", "statement": "s"}], "gold": []}]},
     {"scores": {}}, "lowercase id"),
    ({"cases": [{"id": "c", "turn": "t", "candidates": [{"id": "a", "statement": "s"}], "gold": ["b"]}]},
     {"scores": {}}, "gold"),
    ({"cases": [{"id": "c", "turn": "t", "candidates": [{"id": "a", "statement": "s"}], "gold": []}]},
     {"scores": {"c": {"z": 0.5}}}, "unknown candidate"),
    ({"cases": [{"id": "c", "turn": "t", "candidates": [{"id": "a", "statement": "s"}], "gold": []}]},
     {"scores": {"c": {"a": 1.5}}}, "0..1"),
    ({"cases": [{"id": "c", "turn": "t", "candidates": [{"id": "a", "statement": "s"}], "gold": []}]},
     {"scores": {"other": {}}}, "does not have"),
], ids=lambda _: "")
def test_a_bad_fixture_is_refused(tmp_path, capsys, cases, scores, message):
    (tmp_path / "c.json").write_text(json.dumps(cases))
    (tmp_path / "s.json").write_text(json.dumps(scores))
    code = cli.main(["selector-eval", "--cases", str(tmp_path / "c.json"), "--scores", str(tmp_path / "s.json")])
    error = json.loads(capsys.readouterr().out)
    assert code == 2 and error["status"] == "error" and message in error["error"]


def test_the_fixture_loads_whole():
    cases = load_cases(FIXTURE / "cases.json")
    scores = load_scores(FIXTURE / "scores.json", cases)
    assert [case["id"] for case in cases] == ["release-notes", "checkout-tests", "garden-chart", "thanks-only"]
    assert set(scores) == {case["id"] for case in cases}
