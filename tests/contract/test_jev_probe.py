"""The 24-call probe runner prints its plan and sends nothing unless every condition
to send holds. Every case is synthetic, and every request goes to a fake.
"""

from __future__ import annotations

import importlib.util
import io
import json
import time
from pathlib import Path

import pytest

from imprint.config import DECIDE_PURPOSES_KEY
from imprint.decide import calls_today, decision_log_path

ROOT = Path(__file__).parents[2]
PROBE = ROOT / "tools" / "jev_probe" / "run_probe.py"
KEY_ENV = "PROBE_TEST_KEY"


# -- the probe runner ----------------------------------------------------------------

@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("imprint_test_jev_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fake:
    def __init__(self, fail_at: int | None = None):
        self.calls: list[dict] = []
        self.fail_at = fail_at

    def __call__(self, endpoint, body, headers, timeout):
        self.calls.append(json.loads(body))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise TimeoutError
        answers = {}
        for name, question in self.calls[-1]["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.5}
            elif question["type"] == "choice":
                answers[name] = {"type": "choice", "choice": "irrelevant", "confidence": 0.4,
                                 "probabilities": {"irrelevant": 0.6, "required_here": 0.4}}
            else:
                answers[name] = {"type": "score", "score": 0.7, "confidence": 0.3,
                                 "probabilities": [0.4, 0.5, 0.1, 0.0]}
        return json.dumps({"model": "jev-1.13.0", "answers": answers,
                           "usage": {"input_tokens": 100, "output_tokens": 0}}).encode()


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """Every condition to send holds, so each test can take one away."""
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.delenv("IMPRINT_DECIDE", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-probe-key")
    return write_probe_config(tmp_path, {"probe": {"mode": "shadow", "daily_cap": 24}})


def write_probe_config(tmp_path: Path, purposes: dict, **jev: object) -> Path:
    path = tmp_path / "probe-config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "probe",
        "node_id": "primary", "compiler": True,
        "selector": {"jev": {"api_key_env": KEY_ENV, **jev}}, DECIDE_PURPOSES_KEY: purposes,
    }))
    return path


def run_probe(probe, argv, transport) -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = probe.main(argv, transport=transport, stdout=out)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def test_the_plan_is_exactly_the_approved_24_calls(probe):
    calls = probe.plan()
    assert len(calls) == probe.PLANNED_CALLS == 24 and probe.MODEL == "jev-1.13.0" and probe.PURPOSE == "probe"
    scaling, primitives = calls[:12], calls[12:]
    assert [(c["state_kind"], len(c["questions"])) for c in scaling] == [
        (length, count) for _ in (1, 2) for count in (1, 10, 150) for length in ("short", "long")
    ]
    assert all(c["kind"] == "scaling" and {q.type for q in c["questions"].values()} == {"noul"} for c in scaling)
    short, long = len(scaling[0]["state"]), len(scaling[1]["state"])
    assert 300 <= short <= 600 and 3600 <= long <= 5000  # about 100 and about 1,000 tokens
    assert len({q.instructions for q in scaling[4]["questions"].values()}) == 150
    labels = [c["label"] for c in primitives]
    assert (labels.count("applicable"), labels.count("unrelated")) == (4, 4)
    assert labels.count("conflicting") + labels.count("insufficient") == 4
    for item in primitives:
        assert {name: q.type for name, q in item["questions"].items()} == {
            "applies": "noul", "relation": "choice", "impact": "score",
        }
        assert item["questions"]["relation"].names == (
            "required_here", "conflicts_with_higher_instruction", "irrelevant", "not_enough_evidence",
        )
        assert len(item["questions"]["impact"].levels) == 4
    assert [c["call"] for c in calls] == list(range(1, 25))


def test_by_default_it_prints_the_plan_and_sends_nothing(probe, tmp_path, ready):
    fake = Fake()
    code, lines = run_probe(probe, ["--config", str(ready)], fake)
    assert code == 0 and fake.calls == []
    plan = [line["plan"] for line in lines if "plan" in line]
    assert len(plan) == 24 and all(line["request_bytes"] > 0 for line in plan)
    assert lines[-1]["sent"] == 0 and lines[-1]["calls"] == 24
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("take_away", [
    "flag_missing_count", "count_23", "count_25", "no_config", "probe_off", "probe_absent", "probe_unreadable",
    "cap_short", "cap_spent", "no_key", "bad_key_env", "blank_key_file", "decide_switch", "selector_switch",
    "bad_config",
])
def test_it_refuses_to_send_without_every_condition(probe, tmp_path, monkeypatch, ready, take_away):
    argv = ["--config", str(ready), "--send-approved-calls", "24"]
    if take_away == "flag_missing_count":
        with pytest.raises(SystemExit):
            probe.main(["--config", str(ready), "--send-approved-calls"], transport=Fake(), stdout=io.StringIO())
        return
    if take_away in {"count_23", "count_25"}:
        argv[-1] = take_away[-2:]
    elif take_away == "no_config":
        argv = ["--send-approved-calls", "24"]
    elif take_away == "probe_off":
        write_probe_config(tmp_path, {"probe": {"mode": "off", "daily_cap": 24}})
    elif take_away == "probe_absent":
        write_probe_config(tmp_path, {"event_match": {"mode": "shadow", "daily_cap": 24}})
    elif take_away == "probe_unreadable":
        write_probe_config(tmp_path, {"probe": {"mode": "shadow", "daily_cap": 24, "endpoint": "https://elsewhere.invalid"}})
    elif take_away == "cap_short":
        write_probe_config(tmp_path, {"probe": {"mode": "shadow", "daily_cap": 23}})
    elif take_away == "cap_spent":
        from imprint.decide.client import _reserve, _today

        assert _reserve(tmp_path / "data" / "probe", "probe", 24, _today(), time.monotonic() + 5, time.monotonic) is None
    elif take_away == "no_key":
        monkeypatch.delenv(KEY_ENV)
    elif take_away == "bad_key_env":
        monkeypatch.setenv(KEY_ENV, "not a usable key")
    elif take_away == "blank_key_file":
        monkeypatch.delenv(KEY_ENV)
        key_file = tmp_path / "jev.key"
        key_file.write_text("\n")
        key_file.chmod(0o600)
        write_probe_config(tmp_path, {"probe": {"mode": "shadow", "daily_cap": 24}}, api_key_file=str(key_file))
    elif take_away == "decide_switch":
        monkeypatch.setenv("IMPRINT_DECIDE", "off")
    elif take_away == "selector_switch":
        monkeypatch.setenv("IMPRINT_SELECTOR", "off")
    elif take_away == "bad_config":
        ready.write_text("{not json")
    fake = Fake()
    code, lines = run_probe(probe, argv, fake)
    assert code == 2 and fake.calls == []
    (line,) = lines
    assert line["sent"] == 0 and line["refused"]
    assert not decision_log_path(tmp_path / "data" / "probe", "probe").exists()


def test_with_every_condition_it_sends_the_24_calls_once(probe, tmp_path, ready):
    fake = Fake()
    code, lines = run_probe(probe, ["--config", str(ready), "--send-approved-calls", "24"], fake)
    assert code == 0 and len(fake.calls) == 24
    assert {call["model"] for call in fake.calls} == {"jev-1.13.0"}
    assert lines[-1] == {"completed": 24, "sent": 24}
    results = lines[:-1]
    assert [line["call"] for line in results] == list(range(1, 25))
    assert all(line["outcome"] == "answered" and line["mode"] == "shadow" for line in results)
    assert results[12]["answers"]["relation"]["choice"] == "irrelevant"
    assert results[12]["shapes"][0]["count"] == 1
    assert calls_today(tmp_path / "data" / "probe", "probe") == 24
    text = json.dumps(lines)
    cases = probe.load_cases()
    for case in cases["primitive_cases"]:
        assert case["turn"] not in text and case["judgment"] not in text
    assert cases["states"]["short"] not in text and "ferry" not in text
    log = decision_log_path(tmp_path / "data" / "probe", "probe").read_text()
    assert "ferry" not in log and "Judgment:" not in log and len(log.splitlines()) == 24


def test_it_stops_at_the_first_error_and_never_retries(probe, tmp_path, ready):
    fake = Fake(fail_at=3)
    code, lines = run_probe(probe, ["--config", str(ready), "--send-approved-calls", "24"], fake)
    assert code == 1 and len(fake.calls) == 3
    assert lines[-1] == {"stopped_at": 3, "sent": 3, "reason": "timeout"}
    assert [line["outcome"] for line in lines[:-1]] == ["answered", "answered", "fallback"]


def test_the_shipped_probe_content_is_synthetic():
    text = (ROOT / "tools" / "jev_probe" / "cases.json").read_text(encoding="utf-8")
    assert "synthetic" in text.lower() or "invented" in text.lower()
    for marker in ("/Users/", "@", "http://", "https://"):
        assert marker not in text
