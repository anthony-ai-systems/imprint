"""The typed-decision module, offline: every request goes to a fake transport.

Every state, instruction and description here is invented.
"""

from __future__ import annotations

import copy
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.error

import pytest

from imprint.config import DECIDE_PURPOSES, DECIDE_PURPOSES_KEY, DEFAULTS, decide_setting, load_config, parse_config
from imprint.decide import (
    PURPOSES,
    REASONS,
    ChoiceAnswer,
    NoulAnswer,
    ScoreAnswer,
    ask,
    calls_today,
    choice,
    counter_path,
    decision_log_path,
    enabled_purposes,
    encode_request,
    noul,
    score,
)
from imprint.decide.questions import Choice, Noul, Score

KEY_ENV = "DECIDE_TEST_KEY"
DAY = "2026-11-19"
SECRET_STATE = "zebra-state-sentinel the ticket asks for a refund"
SECRET_INSTRUCTIONS = "quokka-instruction-sentinel Does the ticket ask for money back?"
SECRET_DESCRIPTION = "narwhal-description-sentinel"
SECRET_LEVEL = "axolotl-level-sentinel"
SECRET_KEY = "synthetic-decide-key-sentinel"
OPTIONS = {
    "refund": f"The ticket asks for money back. {SECRET_DESCRIPTION}",
    "repair": "The ticket asks for the item to be fixed.",
    "other": "Anything else.",
}
LEVELS = ("Calm.", "Annoyed.", f"Angry. {SECRET_LEVEL}")


@pytest.fixture(autouse=True)
def decide_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.delenv("IMPRINT_DECIDE", raising=False)
    monkeypatch.setenv(KEY_ENV, SECRET_KEY)


def config(purposes: dict | None = None, **jev_values) -> dict:
    return parse_config({
        "selector": {"jev": {"api_key_env": KEY_ENV, **jev_values}},
        DECIDE_PURPOSES_KEY: purposes if purposes is not None else {"probe": {"mode": "shadow", "daily_cap": 10}},
    })


def questions() -> dict:
    return {
        "asks_refund": noul(SECRET_INSTRUCTIONS),
        "kind": choice("Which kind of request is this?", OPTIONS),
        "temper": score("How upset is the writer?", LEVELS),
    }


class Fake:
    """Answers each sent question with ``reply(name, question)``, or as told."""

    def __init__(self, reply=None, *, raw=None, error=None, delay=0.0, envelope=None):
        self.calls: list[dict] = []
        self.reply = reply or default_reply
        self.raw, self.error, self.delay, self.envelope = raw, error, delay, envelope

    def __call__(self, endpoint, body, headers, timeout):
        self.calls.append({"endpoint": endpoint, "body": json.loads(body), "bytes": body, "headers": dict(headers),
                           "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        sent = self.calls[-1]["body"]["questions"]
        answers = {}
        for name, question in sent.items():
            value = self.reply(name, question)
            if value is not None:
                answers[name] = value
        envelope = self.envelope if self.envelope is not None else {
            "model": "jev-1.13.0", "usage": {"input_tokens": 40, "output_tokens": 9},
        }
        return json.dumps({**envelope, "answers": answers}).encode("utf-8")


def default_reply(name, question):
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.81}
    if question["type"] == "choice":
        return {"type": "choice", "choice": "refund", "confidence": 0.7,
                "probabilities": {"refund": 0.8, "repair": 0.15, "other": 0.05}, "extra": "ignored"}
    return {"type": "score", "score": 1.6, "confidence": 0.5, "legend": {"0": "Calm."},
            "probabilities": [0.1, 0.2, 0.7]}


def call(tmp_path, fake=None, *, purpose="probe", cfg=None, qs=None, state=SECRET_STATE, **kwargs):
    return ask(purpose, state, qs if qs is not None else questions(), config=cfg or config(), root=tmp_path / "op",
               transport=fake, day=kwargs.pop("day", DAY), **kwargs)


def log_lines(tmp_path, purpose="probe") -> list[dict]:
    path = decision_log_path(tmp_path / "op", purpose)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# -- the primitives' request shapes -----------------------------------------------

def test_each_primitive_has_its_documented_request_shape():
    assert noul("Is it raining?").request() == {"type": "noul", "instructions": "Is it raining?"}
    assert choice("Which?", {"a": "First.", "b": "Second."}).request() == {
        "type": "choice", "instructions": "Which?", "criteria": {"a": "First.", "b": "Second."},
    }
    assert score("How much?", ["None.", "Some.", "All."]).request() == {
        "type": "score", "instructions": "How much?", "criteria": ["None.", "Some.", "All."],
    }


def test_choice_keeps_the_option_order_it_was_given():
    built = choice("Which?", {"zeta": "Last letter.", "alpha": "First letter."})
    assert list(built.request()["criteria"]) == ["zeta", "alpha"]


@pytest.mark.parametrize("build", [
    lambda: noul(""),
    lambda: noul("   "),
    lambda: noul("x" * 4001),
    lambda: noul(None),
    lambda: choice("Which?", {}),
    lambda: choice("Which?", {"Bad Name": "Spaces."}),
    lambda: choice("Which?", {"ok": ""}),
    lambda: choice("Which?", {"ok": 3}),
    lambda: choice("Which?", {f"o{n}": "An option." for n in range(256)}),
    lambda: choice("Which?", [("a", "A.")]),
    lambda: choice("", {"a": "A."}),
    lambda: score("How much?", []),
    lambda: score("How much?", [f"Level {n}." for n in range(11)]),
    lambda: score("How much?", ["Fine.", ""]),
    lambda: score("How much?", "abc"),
], ids=lambda _: "")
def test_builders_refuse_what_the_api_would_not_take(build):
    with pytest.raises(ValueError):
        build()


def test_the_body_holds_model_state_and_questions_and_nothing_else(tmp_path):
    fake = Fake()
    result = call(tmp_path, fake)
    assert result.outcome == "answered"
    (sent,) = fake.calls
    assert sent["endpoint"] == DEFAULTS["selector"]["jev"]["endpoint"]
    assert sent["body"] == {
        "model": "jev-1.13.0",
        "state": SECRET_STATE,
        "questions": {
            "asks_refund": {"type": "noul", "instructions": SECRET_INSTRUCTIONS},
            "kind": {"type": "choice", "instructions": "Which kind of request is this?", "criteria": OPTIONS},
            "temper": {"type": "score", "instructions": "How upset is the writer?", "criteria": list(LEVELS)},
        },
    }
    assert sent["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["headers"]["User-Agent"].startswith("imprint-local/")
    assert sent["bytes"] == encode_request("jev-1.13.0", SECRET_STATE, questions())
    assert result.request_bytes == len(sent["bytes"])


def test_a_model_pin_and_an_object_state_are_sent_as_given(tmp_path):
    fake = Fake()
    state = {"ticket": {"subject": "Broken lamp", "lines": ["It flickers.", "Please help."]}}
    call(tmp_path, fake, state=state, model="jev-1.14.0")
    assert (fake.calls[0]["body"]["model"], fake.calls[0]["body"]["state"]) == ("jev-1.14.0", state)


# -- reading answers ---------------------------------------------------------------

def test_typed_answers_come_back_for_each_primitive(tmp_path):
    result = call(tmp_path, Fake())
    assert result.answers == {
        "asks_refund": NoulAnswer(0.81),
        "kind": ChoiceAnswer("refund", 0.7, {"refund": 0.8, "repair": 0.15, "other": 0.05}),
        "temper": ScoreAnswer(1.6, 0.5, {0: 0.1, 1: 0.2, 2: 0.7}),
    }
    assert (result.dropped, result.model_echoed, result.usage) == ((), "jev-1.13.0", {"input_tokens": 40, "output_tokens": 9})
    assert result.shapes["kind"] == {
        "choice": "str", "confidence": "number", "extra": "str", "probabilities": "object", "type": "str",
    }


def test_probabilities_in_request_order_or_by_level_number_are_read():
    assert Choice("Which?", (("a", "A."), ("b", "B."))).parse(
        {"type": "choice", "choice": "b", "probabilities": [0.25, 0.75]},
    ) == ChoiceAnswer("b", None, {"a": 0.25, "b": 0.75})
    assert Score("How?", ("Low.", "Mid.", "High.")).parse(
        {"type": "score", "score": 2, "probabilities": {"0": 0.1, "2": 0.9}},
    ) == ScoreAnswer(2.0, None, {0: 0.1, 2: 0.9})
    assert Score("How?", ("Low.", "Mid.", "High.")).parse(
        {"type": "score", "score": 2, "probabilities": [0.1, 0.0, 0.9]},
    ) == ScoreAnswer(2.0, None, {0: 0.1, 1: 0.0, 2: 0.9})


NOUL = Noul("Is it so?")
CHOICE = Choice("Which?", (("a", "A."), ("b", "B.")))
SCORE = Score("How?", ("Low.", "Mid.", "High."))


@pytest.mark.parametrize("question, answer", [
    (NOUL, {"type": "noul", "noul": 1.5}),
    (NOUL, {"type": "noul", "noul": -0.1}),
    (NOUL, {"type": "noul", "noul": True}),
    (NOUL, {"type": "noul", "noul": "0.5"}),
    (NOUL, {"type": "noul", "noul": float("inf")}),
    (NOUL, {"noul": 0.5}),
    (NOUL, {"type": "score", "noul": 0.5}),
    (NOUL, [0.5]),
    (CHOICE, {"type": "choice", "choice": "c"}),
    (CHOICE, {"type": "choice", "choice": None}),
    (CHOICE, {"type": "choice", "choice": "a", "confidence": 1.2}),
    (CHOICE, {"type": "choice", "choice": "a", "confidence": "high"}),
    (CHOICE, {"type": "choice", "choice": "a", "probabilities": {"a": 0.5, "z": 0.5}}),
    (CHOICE, {"type": "choice", "choice": "a", "probabilities": {"a": 2}}),
    (CHOICE, {"type": "choice", "choice": "a", "probabilities": [0.5]}),
    (CHOICE, {"type": "choice", "choice": "a", "probabilities": "a"}),
    (CHOICE, {"type": "noul", "choice": "a"}),
    (SCORE, {"type": "score", "score": 3}),
    (SCORE, {"type": "score", "score": -1}),
    (SCORE, {"type": "score", "score": "2"}),
    (SCORE, {"type": "score", "score": False}),
    (SCORE, {"type": "score"}),
    (SCORE, {"type": "score", "score": 1, "confidence": -0.5}),
    (SCORE, {"type": "score", "score": 1, "probabilities": [0.5, 0.5]}),
    (SCORE, {"type": "score", "score": 1, "probabilities": {"3": 0.5}}),
    (SCORE, {"type": "score", "score": 1, "probabilities": {"high": 0.5}}),
    (SCORE, {"type": "score", "score": 1, "probabilities": [0.5, 0.5, None]}),
], ids=lambda _: "")
def test_a_malformed_answer_is_rejected(question, answer):
    assert question.parse(answer) is None


def test_unknown_fields_are_ignored():
    assert NOUL.parse({"type": "noul", "noul": 0.4, "confidence": "anything", "note": [1]}) == NoulAnswer(0.4)
    assert SCORE.parse({"type": "score", "score": 0.5, "legend": ["x"], "future": {}}) == ScoreAnswer(0.5)


@pytest.mark.parametrize("key", ["²", "١", "01", "-1", " 1", "1.0", ""], ids=lambda _: "")
def test_a_level_key_that_is_not_a_plain_level_number_is_rejected_not_raised(key):
    assert SCORE.parse({"type": "score", "score": 1, "probabilities": {key: 0.5}}) is None


def test_one_answer_that_cannot_be_read_never_costs_the_others(tmp_path):
    def reply(name, question):
        value = default_reply(name, question)
        return {**value, "probabilities": {"²": 0.5}} if name == "temper" else value

    result = call(tmp_path, Fake(reply))
    assert (result.outcome, set(result.answers), result.dropped) == ("answered", {"asks_refund", "kind"}, ("temper",))


class Exploding(Noul):
    def parse(self, answer):
        raise RuntimeError("synthetic parser fault")


def test_a_parser_that_raises_drops_only_its_own_answer(tmp_path):
    result = call(tmp_path, Fake(), qs={**questions(), "fragile": Exploding("Is it so?")})
    assert (result.outcome, set(result.answers), result.dropped) == (
        "answered", {"asks_refund", "kind", "temper"}, ("fragile",),
    )


def test_a_sparse_probability_object_is_kept_and_logged_as_sent(tmp_path):
    def reply(name, question):
        value = default_reply(name, question)
        if name == "kind":
            return {**value, "probabilities": {"refund": 0.8}}
        if name == "temper":
            return {**value, "probabilities": {"2": 0.7}}
        return value

    result = call(tmp_path, Fake(reply))
    assert result.answers["kind"].probabilities == {"refund": 0.8}
    assert result.answers["temper"].probabilities == {2: 0.7}
    (line,) = log_lines(tmp_path)
    assert line["answers"]["kind"]["probabilities"] == {"refund": 0.8}
    assert line["answers"]["temper"]["probabilities"] == {"2": 0.7}


def test_a_partial_answer_set_keeps_what_is_valid_and_names_the_rest(tmp_path):
    def reply(name, question):
        if name == "asks_refund":
            return {"type": "noul", "noul": 0.2}
        if name == "kind":
            return {"type": "choice", "choice": "teleport"}  # an option never sent
        return None  # temper left out

    fake = Fake(reply)
    result = call(tmp_path, fake)
    assert (result.outcome, set(result.answers), result.dropped) == ("answered", {"asks_refund"}, ("kind", "temper"))
    assert set(result.shapes) == {"asks_refund", "kind"}
    (line,) = log_lines(tmp_path)
    assert line["dropped"] == ["kind", "temper"]
    assert line["dropped_shapes"] == {"kind": {"choice": "str", "type": "str"}}


def test_answers_to_questions_never_sent_are_ignored(tmp_path):
    fake = Fake(raw=json.dumps({"answers": {
        "asks_refund": {"type": "noul", "noul": 0.6}, "smuggled": {"type": "noul", "noul": 1.0},
    }}).encode())
    result = call(tmp_path, fake)
    assert set(result.answers) == {"asks_refund"} and "smuggled" not in result.shapes


@pytest.mark.parametrize("raw", [
    b"not json", b"[]", b'{"answers": []}', b'{"answers": {}}', b"\xff\xfe",
    json.dumps({"answers": {"asks_refund": {"type": "noul", "noul": 7}}}).encode(),
], ids=lambda _: "")
def test_no_valid_answer_at_all_is_a_bad_response(tmp_path, raw):
    result = call(tmp_path, Fake(raw=raw))
    assert (result.outcome, result.reason, result.answers, result.request_sent) == (
        "fallback", "bad_response", {}, True,
    )


# -- failures never raise ----------------------------------------------------------

@pytest.mark.parametrize("error, reason", [
    (urllib.error.URLError("synthetic refusal"), "http_error"),
    (urllib.error.HTTPError("https://example.invalid", 429, "Too Many", {}, None), "http_error"),
    (TimeoutError(), "timeout"),
    (RuntimeError("synthetic"), "exception"),
], ids=lambda value: "" if not isinstance(value, str) else value)
def test_transport_failures_are_closed_reasons(tmp_path, error, reason):
    result = call(tmp_path, Fake(error=error))
    assert (result.outcome, result.reason) == ("fallback", reason)
    assert reason in REASONS


def test_the_deadline_holds(tmp_path):
    fake = Fake(delay=1.0)
    begun = time.monotonic()
    result = call(tmp_path, fake, timeout_seconds=0.2)
    assert (result.outcome, result.reason) == ("fallback", "timeout")
    assert time.monotonic() - begun < 0.9


def test_the_default_transport_is_the_selectors_and_the_guard_stops_it(tmp_path):
    result = call(tmp_path, None, timeout_seconds=2)
    assert (result.outcome, result.reason, result.request_sent) == ("fallback", "http_error", True)


def test_the_selectors_transport_is_looked_up_per_call(tmp_path, monkeypatch):
    from imprint.retrieve import jev

    fake = Fake()
    monkeypatch.setattr(jev, "http_post", fake)
    assert call(tmp_path, None).outcome == "answered"
    assert len(fake.calls) == 1


@pytest.mark.parametrize("change", [
    {"qs": {"Bad Name": noul("Is it?")}},
    {"qs": {"ok": {"type": "noul", "instructions": "A raw dict."}}},
    {"qs": {}},
    {"qs": {f"q{n}": noul("Is it?") for n in range(1001)}},
    {"state": ""},
    {"state": "x" * 16001},
    {"state": {"value": float("nan")}},
    {"state": {}},
    {"state": ["a", "list"]},
    {"state": None},
    {"model": "bad model name"},
    {"timeout_seconds": 0},
    {"timeout_seconds": 31},
    {"timeout_seconds": True},
    {"day": "yesterday"},
], ids=lambda _: "")
def test_an_invalid_request_is_refused_before_anything_is_sent(tmp_path, change):
    fake = Fake()
    result = call(tmp_path, fake, **change)
    assert (result.outcome, result.reason, result.request_sent) == ("fallback", "invalid_request", False)
    assert fake.calls == []
    assert calls_today(tmp_path / "op", "probe", day=DAY) == 0


def test_a_broken_config_is_a_fallback_not_an_exception(tmp_path):
    cfg = config()
    del cfg["selector"]
    fake = Fake()
    result = call(tmp_path, fake, cfg=cfg)
    assert (result.outcome, fake.calls) == ("fallback", [])


def test_a_missing_key_sends_nothing_and_takes_no_call(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    fake = Fake()
    result = call(tmp_path, fake)
    assert (result.outcome, result.reason, fake.calls) == ("fallback", "missing_key", [])
    assert calls_today(tmp_path / "op", "probe", day=DAY) == 0


# -- purposes and modes -----------------------------------------------------------

def test_the_purposes_are_the_closed_list():
    assert PURPOSES == DECIDE_PURPOSES == (
        "selector", "capture_classify", "event_match", "completion_check", "draft_check", "action_guard", "probe",
    )


def test_the_default_config_enables_nothing(tmp_path):
    assert DEFAULTS[DECIDE_PURPOSES_KEY] == {}
    default = parse_config({"selector": {"jev": {"api_key_env": KEY_ENV}}})
    fake = Fake()
    for purpose in PURPOSES:
        result = call(tmp_path, fake, purpose=purpose, cfg=default)
        assert (result.outcome, result.reason, result.mode) == ("disabled", "purpose_off", "off")
    assert fake.calls == []
    assert not (tmp_path / "op").exists()


@pytest.mark.parametrize("purposes", [
    {"probe": {"mode": "off", "daily_cap": 10}},
    {"probe": {"daily_cap": 10}},
    {"event_match": {"mode": "enforce", "daily_cap": 10}},
], ids=["mode_off", "mode_absent", "another_purpose"])
def test_a_disabled_purpose_makes_no_request_and_writes_nothing(tmp_path, purposes):
    fake = Fake()
    result = call(tmp_path, fake, cfg=config(purposes))
    assert (result.outcome, result.reason, result.request_sent) == ("disabled", "purpose_off", False)
    assert fake.calls == [] and not (tmp_path / "op").exists()


def test_an_unknown_purpose_is_disabled(tmp_path):
    fake = Fake()
    result = call(tmp_path, fake, purpose="mind_reading")
    assert (result.purpose, result.outcome, result.reason) == ("unknown", "disabled", "unknown_purpose")
    assert fake.calls == []


@pytest.mark.parametrize("name, value", [
    ("IMPRINT_DECIDE", "off"), ("IMPRINT_DECIDE", "1"), ("IMPRINT_SELECTOR", "off"), ("IMPRINT_SELECTOR", "0"),
])
def test_the_environment_can_only_turn_purposes_off(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    fake = Fake()
    result = call(tmp_path, fake)
    assert (result.outcome, result.reason) == ("disabled", "switched_off")
    assert fake.calls == []


@pytest.mark.parametrize("value", ["jev", "shadow"])
def test_a_selector_value_that_keeps_the_config_keeps_the_purpose(tmp_path, monkeypatch, value):
    monkeypatch.setenv("IMPRINT_SELECTOR", value)
    assert call(tmp_path, Fake()).outcome == "answered"


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_shadow_and_enforce_are_reported_and_answers_flagged(tmp_path, mode):
    result = call(tmp_path, Fake(), cfg=config({"probe": {"mode": mode, "daily_cap": 5}}))
    assert result.outcome == "answered" and result.answers
    assert (result.mode, result.shadow, result.enforce) == (mode, mode == "shadow", mode == "enforce")
    assert log_lines(tmp_path)[0]["mode"] == mode


# -- the daily cap ------------------------------------------------------------------

def test_the_daily_cap_stops_calls_locally(tmp_path):
    fake = Fake()
    cfg = config({"probe": {"mode": "enforce", "daily_cap": 2}})
    outcomes = [call(tmp_path, fake, cfg=cfg) for _ in range(3)]
    assert [(r.outcome, r.reason) for r in outcomes] == [("answered", None), ("answered", None), ("capped", "cap_reached")]
    assert len(fake.calls) == 2
    assert calls_today(tmp_path / "op", "probe", day=DAY) == 2
    assert json.loads(counter_path(tmp_path / "op").read_text()) == {"calls": {"probe": 2}, "day": DAY}


def test_a_cap_of_zero_never_sends(tmp_path):
    fake = Fake()
    result = call(tmp_path, fake, cfg=config({"probe": {"mode": "enforce"}}))
    assert (result.outcome, result.reason, fake.calls) == ("capped", "cap_reached", [])


def test_a_new_day_starts_a_new_count(tmp_path):
    fake = Fake()
    cfg = config({"probe": {"mode": "enforce", "daily_cap": 1}})
    assert call(tmp_path, fake, cfg=cfg, day="2026-11-19").outcome == "answered"
    assert call(tmp_path, fake, cfg=cfg, day="2026-11-19").outcome == "capped"
    assert call(tmp_path, fake, cfg=cfg, day="2026-11-20").outcome == "answered"
    assert len(fake.calls) == 2


def test_each_purpose_has_its_own_count(tmp_path):
    fake = Fake()
    cfg = config({"probe": {"mode": "enforce", "daily_cap": 1}, "draft_check": {"mode": "shadow", "daily_cap": 1}})
    assert call(tmp_path, fake, cfg=cfg).outcome == "answered"
    assert call(tmp_path, fake, cfg=cfg, purpose="draft_check").outcome == "answered"
    assert call(tmp_path, fake, cfg=cfg).outcome == "capped"


@pytest.mark.parametrize("content", [b"not json", b'{"day": "2026-11-19"}', b'{"day": 1, "calls": {}}',
                                     b'{"day": "2026-11-19", "calls": {"probe": -1}}', b"x" * 5000])
def test_a_counter_it_cannot_trust_fails_closed(tmp_path, content):
    path = counter_path(tmp_path / "op")
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    fake = Fake()
    result = call(tmp_path, fake)
    assert (result.outcome, result.reason, fake.calls) == ("capped", "cap_unavailable", [])
    assert calls_today(tmp_path / "op", "probe", day=DAY) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock")
def test_a_held_counter_lock_gives_up_inside_the_deadline(tmp_path):
    import fcntl

    logs = tmp_path / "op" / "logs"
    logs.mkdir(parents=True)
    fake = Fake()
    results: list = []
    with open(logs / "decide-calls.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        worker = threading.Thread(target=lambda: results.append(call(tmp_path, fake, timeout_seconds=0.3)), daemon=True)
        worker.start()
        worker.join(5)
        stuck = worker.is_alive()
    worker.join(5)
    assert not stuck, "ask waited on the counter lock past its deadline"
    (result,) = results
    assert (result.outcome, result.reason, fake.calls) == ("capped", "cap_unavailable", [])
    assert calls_today(tmp_path / "op", "probe", day=DAY) == 0


def test_without_any_file_lock_the_cap_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    fake = Fake()
    result = call(tmp_path, fake, cfg=config({"probe": {"mode": "enforce", "daily_cap": 1}}))
    assert (result.outcome, result.reason, fake.calls) == ("capped", "cap_unavailable", [])
    assert calls_today(tmp_path / "op", "probe", day=DAY) == 0


@pytest.mark.skipif(os.name == "nt", reason="stands in for the Windows lock with POSIX flock")
def test_the_windows_lock_keeps_the_cap_between_processes(tmp_path, monkeypatch):
    import fcntl
    import types

    events: list[str] = []

    def locking(descriptor, mode, size):
        assert size == 1 and os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        if mode == windows.LK_UNLCK:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            events.append("released")
            return
        assert mode == windows.LK_NBLCK
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OSError(36, "Resource deadlock avoided") from None  # what msvcrt raises when held
        events.append("taken")

    windows = types.SimpleNamespace(LK_UNLCK=0, LK_NBLCK=2, locking=locking)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", windows)
    logs = tmp_path / "op" / "logs"
    logs.mkdir(parents=True)
    fake = Fake()
    cfg = config({"probe": {"mode": "enforce", "daily_cap": 1}})
    with open(logs / "decide-calls.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = call(tmp_path, fake, cfg=cfg, timeout_seconds=0.3)
    assert (result.outcome, result.reason, fake.calls, events) == ("capped", "cap_unavailable", [], [])
    assert call(tmp_path, fake, cfg=cfg).outcome == "answered"
    assert call(tmp_path, fake, cfg=cfg).reason == "cap_reached"
    assert events == ["taken", "released", "taken", "released"] and len(fake.calls) == 1


def test_a_call_already_out_of_time_takes_nothing_from_the_cap(tmp_path):
    ticks = iter([0.0])
    fake = Fake()
    result = call(tmp_path, fake, clock=lambda: next(ticks, 100.0))
    assert (result.outcome, result.reason, fake.calls) == ("fallback", "timeout", [])
    assert calls_today(tmp_path / "op", "probe", day=DAY) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_counter_and_log_are_private(tmp_path):
    call(tmp_path, Fake())
    assert counter_path(tmp_path / "op").stat().st_mode & 0o077 == 0
    assert decision_log_path(tmp_path / "op", "probe").stat().st_mode & 0o077 == 0


# -- the decision log ----------------------------------------------------------------

def test_the_decision_log_is_content_free(tmp_path):
    call(tmp_path, Fake())
    call(tmp_path, Fake(error=urllib.error.URLError("synthetic refusal")))
    raw = decision_log_path(tmp_path / "op", "probe").read_text()
    for secret in (SECRET_STATE, "zebra", SECRET_INSTRUCTIONS, "quokka", SECRET_DESCRIPTION, "narwhal",
                   SECRET_LEVEL, "axolotl", SECRET_KEY, "Which kind", "How upset", "Calm."):
        assert secret not in raw
    answered, failed = log_lines(tmp_path)
    assert set(answered) == {
        "answers", "dropped", "elapsed_ms", "mode", "model_echoed", "outcome", "purpose", "questions", "reason",
        "request_bytes", "request_sent", "ts", "usage",
    }
    assert answered["questions"] == {"asks_refund": "noul", "kind": "choice", "temper": "score"}
    assert answered["answers"] == {
        "asks_refund": {"type": "noul", "noul": 0.81},
        "kind": {"type": "choice", "choice": "refund", "confidence": 0.7,
                 "probabilities": {"other": 0.05, "refund": 0.8, "repair": 0.15}},
        "temper": {"type": "score", "score": 1.6, "confidence": 0.5, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
    }
    assert (answered["purpose"], answered["outcome"], answered["reason"], answered["request_sent"]) == (
        "probe", "answered", None, True,
    )
    assert answered["request_bytes"] > 0 and answered["usage"] == {"input_tokens": 40, "output_tokens": 9}
    assert (failed["outcome"], failed["reason"], failed["answers"]) == ("fallback", "http_error", {})


def test_a_capped_call_is_logged_and_a_disabled_one_is_not(tmp_path):
    call(tmp_path, Fake(), cfg=config({"probe": {"mode": "enforce"}}))
    call(tmp_path, Fake(), cfg=config({}))
    (line,) = log_lines(tmp_path)
    assert (line["outcome"], line["reason"], line["request_sent"]) == ("capped", "cap_reached", False)


def test_an_unloggable_name_never_reaches_the_log(tmp_path):
    call(tmp_path, Fake(), qs={"Has Spaces and words": noul("Is it?")})
    (line,) = log_lines(tmp_path)
    assert line["questions"] == {} and "Spaces" not in json.dumps(line)


# -- config ------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    [],
    "probe",
    3,
    {"mind_reading": {"mode": "shadow", "daily_cap": 5}},
    {"probe": "shadow"},
    {"probe": {"mode": "shadow", "daily_cap": 5, "endpoint": "https://elsewhere.invalid"}},
    {"probe": {"mode": "on", "daily_cap": 5}},
    {"probe": {"mode": "shadow", "daily_cap": -1}},
    {"probe": {"mode": "shadow", "daily_cap": 100001}},
    {"probe": {"mode": "shadow", "daily_cap": True}},
    {"probe": {"mode": "shadow", "daily_cap": 2.5}},
], ids=lambda _: "")
def test_an_entry_this_build_cannot_read_loads_and_is_off(tmp_path, value):
    """A later build's purpose or field, or a typo, must not stop the config loading:
    every hook loads it. What this build cannot read sends nothing."""
    cfg = config(value)
    fake = Fake()
    for purpose in PURPOSES:
        assert decide_setting(cfg, purpose) == ("off", 0)
        result = call(tmp_path, fake, purpose=purpose, cfg=cfg)
        assert (result.outcome, result.reason, result.request_sent) == ("disabled", "purpose_off", False)
    assert fake.calls == [] and enabled_purposes(cfg) == ()


def test_an_unreadable_entry_leaves_the_others_alone(tmp_path):
    cfg = config({
        "probe": {"mode": "enforce", "daily_cap": 3},
        "mind_reading": {"mode": "enforce", "daily_cap": 3},
        "draft_check": {"mode": "shadow", "daily_cap": 3, "model": "jev-9.0.0"},
    })
    assert enabled_purposes(cfg) == ("probe",)
    assert call(tmp_path, Fake(), cfg=cfg).outcome == "answered"
    assert call(tmp_path, Fake(), cfg=cfg, purpose="draft_check").reason == "purpose_off"


def test_a_later_builds_purpose_does_not_break_the_hooks(tmp_path, monkeypatch, capsys):
    import imprint.cli as cli

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True,
        DECIDE_PURPOSES_KEY: {
            "mind_reading": {"mode": "enforce", "daily_cap": 5},
            "probe": {"mode": "shadow", "daily_cap": 1, "budget": 3},
        },
    }), encoding="utf-8")
    assert set(load_config(path)[DECIDE_PURPOSES_KEY]) == {"mind_reading", "probe"}
    for action, event in (
        ("session-start", {"session_id": "s", "source": "startup"}),
        ("user-prompt-submit", {"session_id": "s", "prompt": "a synthetic prompt"}),
    ):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        assert cli.main(["--config", str(path), "hook", action]) == 0
        assert json.loads(capsys.readouterr().out).get("status") != "error"


def test_enabled_purposes_are_those_that_can_send():
    cfg = config({
        "probe": {"mode": "shadow", "daily_cap": 1},
        "draft_check": {"mode": "enforce"},
        "action_guard": {"mode": "enforce", "daily_cap": 0},
        "event_match": {"mode": "off", "daily_cap": 5},
        "capture_classify": {"mode": "enforce", "daily_cap": 2},
    })
    assert enabled_purposes(cfg) == ("capture_classify", "probe")
    assert enabled_purposes(config({})) == ()
    assert enabled_purposes(parse_config({})) == ()


def test_no_socket_is_opened_by_a_disabled_purpose(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: opened.append(args))
    call(tmp_path, None, cfg=config({}))
    assert opened == []


# -- health --------------------------------------------------------------------------

def health_metrics(tmp_path, purposes=None, mode="deterministic") -> dict:
    from imprint.health import health_report
    from imprint.store import ImprintStore

    root = tmp_path / "op"
    root.mkdir(parents=True, exist_ok=True)
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    cfg = copy.deepcopy(DEFAULTS)
    cfg["hooks_dir"] = str(tmp_path)
    cfg["selector"]["mode"] = mode
    cfg["selector"]["jev"]["api_key_env"] = KEY_ENV
    if purposes is not None:
        cfg[DECIDE_PURPOSES_KEY] = purposes
    return health_report(root, store, cfg)["metrics"]


def test_with_no_purpose_named_health_is_unchanged(tmp_path):
    metrics = health_metrics(tmp_path)
    assert (metrics["network_state"], metrics["last_transfer_age_seconds"]) == ("offline", -1)
    assert "decide_purposes" not in metrics


def test_an_enabled_purpose_keeps_health_from_saying_offline(tmp_path):
    purposes = {"probe": {"mode": "shadow", "daily_cap": 2}}
    metrics = health_metrics(tmp_path, purposes)
    assert (metrics["network_state"], metrics["last_transfer_age_seconds"]) == ("idle", -1)
    assert metrics["decide_purposes"] == {"enabled": 1, "ignored": 0}
    # A call that never sends is not a transfer.
    call(tmp_path, Fake(), cfg=config({"probe": {"mode": "shadow"}}))
    assert health_metrics(tmp_path, purposes)["network_state"] == "idle"
    assert call(tmp_path, Fake(), cfg=config(purposes)).request_sent
    metrics = health_metrics(tmp_path, purposes)
    assert metrics["network_state"] == "transferred"
    assert 0 <= metrics["last_transfer_age_seconds"] < 60
    # Under jev, the selector's idle state does not hide the purpose's transfer.
    assert health_metrics(tmp_path, purposes, mode="jev")["network_state"] == "transferred"
    # Turned off again, the purpose no longer speaks for the network.
    assert health_metrics(tmp_path, {})["network_state"] == "offline"


def test_a_failed_send_still_counts_as_a_transfer(tmp_path):
    purposes = {"probe": {"mode": "enforce", "daily_cap": 2}}
    assert call(tmp_path, Fake(error=TimeoutError()), cfg=config(purposes)).request_sent
    assert health_metrics(tmp_path, purposes)["network_state"] == "transferred"


def test_health_counts_entries_it_cannot_read_and_does_not_enable_them(tmp_path):
    metrics = health_metrics(tmp_path, {
        "mind_reading": {"mode": "enforce", "daily_cap": 1},
        "draft_check": {"mode": "enforce", "daily_cap": "many"},
        "probe": {"mode": "enforce"},
    })
    assert metrics["network_state"] == "offline"
    assert metrics["decide_purposes"] == {"enabled": 0, "ignored": 2}
