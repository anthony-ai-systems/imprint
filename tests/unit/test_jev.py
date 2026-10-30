"""Unit coverage for the optional Jev relevance selector (offline only)."""

from __future__ import annotations

import copy
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from imprint.config import DEFAULTS, SELECTOR_NODE_TYPES, load_config
from imprint.errors import ValidationError
from imprint.retrieve import jev
from imprint.retrieve.jev import (
    QUESTION_PREFIX,
    STATE_LIMIT,
    STATEMENT_LIMIT,
    JevSelector,
    JevSettings,
    effective_mode,
    egress_text,
    prefilter,
)
from imprint.retrieve.models import RetrievalRecord

KEY_ENV = "JEV_TEST_KEY"


@pytest.fixture(autouse=True)
def isolated_selector_home(tmp_path, monkeypatch):
    """No real key file or key variable can ever be found by these tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)
    return home


def item(record_id: str, statement: str, **overrides) -> RetrievalRecord:
    values = dict(
        record_id=record_id, text=statement, section="general",
        provenance_status="captured", authority_tier="captured_judgment",
        evidence_ids=("urn:imprint:evidence:synthetic",), case_ids=("urn:imprint:case:synthetic",),
        provenance_complete=True, ontology_type="Verdict", judgment_statement=statement,
    )
    values.update(overrides)
    return RetrievalRecord(**values)


def settings(**overrides) -> JevSettings:
    values = dict(endpoint="https://selector.example.invalid/v1/systemone", model="jev-test", api_key_env=KEY_ENV)
    values.update(overrides)
    return JevSettings(**values)


class FakeTransport:
    """Records each request and answers every question with ``scorer``."""

    def __init__(self, scorer=lambda instructions: 0.9, *, raw=None, error=None, delay=0.0):
        self.calls: list[dict] = []
        self.scorer, self.raw, self.error, self.delay = scorer, raw, error, delay

    def __call__(self, endpoint, body, headers, timeout):
        self.calls.append({
            "endpoint": endpoint, "body": json.loads(body), "headers": dict(headers), "timeout": timeout,
        })
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        questions = self.calls[-1]["body"]["questions"]
        return json.dumps({
            "model": "jev-test-echo",
            "answers": {name: {"type": "noul", "noul": self.scorer(q["instructions"])} for name, q in questions.items()},
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }).encode("utf-8")


def keyed(monkeypatch, value="synthetic-test-key"):
    monkeypatch.setenv(KEY_ENV, value)


# -- network guard -----------------------------------------------------------

BLOCKED = "outbound network is disabled for the test suite"


def test_suite_network_guard_is_active():
    # Every call below must be stopped by the suite guard itself, not by a
    # real lookup or connection failing, hence the exact message.
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        with pytest.raises(OSError, match=BLOCKED):
            getattr(socket, name)("api.typesafe.ai")
    with pytest.raises(OSError, match=BLOCKED):
        socket.getnameinfo(("192.0.2.1", 443), 0)
    with pytest.raises(OSError, match=BLOCKED):
        socket.create_connection(("192.0.2.1", 443), timeout=0.1)
    with pytest.raises(OSError, match=BLOCKED):
        socket.getaddrinfo("api.typesafe.ai", 443)
    with pytest.raises(OSError, match=BLOCKED):
        urllib.request.urlopen("https://api.typesafe.ai/v1/systemone", timeout=0.1)
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match=BLOCKED):
            raw.connect(("192.0.2.1", 443))
        with pytest.raises(OSError, match=BLOCKED):
            raw.connect_ex(("192.0.2.1", 443))
    finally:
        raw.close()
    datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OSError, match=BLOCKED):
            datagram.sendto(b"x", ("192.0.2.1", 53))
        if hasattr(datagram, "sendmsg"):
            with pytest.raises(OSError, match=BLOCKED):
                datagram.sendmsg([b"x"], [], 0, ("192.0.2.1", 53))
    finally:
        datagram.close()


def test_default_transport_cannot_reach_the_network(monkeypatch):
    # The real transport is exercised end to end and is stopped by the guard.
    keyed(monkeypatch)
    guard, attempts = socket.create_connection, []

    def spy(address, *args, **kwargs):
        attempts.append(address)
        try:
            return guard(address, *args, **kwargs)
        except OSError as exc:
            attempts.append(str(exc))
            raise

    monkeypatch.setattr(socket, "create_connection", spy)
    result = JevSelector(settings(timeout_seconds=2)).select("release safely", [item("a", "release safely")])
    assert result.outcome == "fallback"
    assert result.fallback_reason == "http_error"
    # One connection attempt (to the endpoint, or a configured proxy), and the
    # guard stopped it.
    assert len(attempts) == 2 and attempts[1] == BLOCKED


# -- prefilter and egress text ----------------------------------------------

def test_prefilter_is_bounded_and_relevance_ordered():
    values = [item("irrelevant", "gardening flowers"), item("relevant", "deploy production safely")]
    assert [x.record_id for x in prefilter("production deploy", values, keep=1)] == ["relevant"]


def test_prefilter_ties_prefer_newer_records_then_id_deterministically():
    values = [
        item("b", "alpha", valid_from="2026-01-01T00:00:00Z"),
        item("a", "alpha", valid_from="2026-01-01T00:00:00Z"),
        item("c", "alpha", valid_from="2026-06-01T00:00:00Z"),
    ]
    assert [x.record_id for x in prefilter("unrelated words", values, keep=3)] == ["c", "a", "b"]
    assert [x.record_id for x in prefilter("unrelated words", list(reversed(values)), keep=2)] == ["c", "a"]


def test_egress_text_uses_only_the_statement_and_is_bounded():
    record = item(
        "r", "Prefer the slower path " * 40 + " see urn:imprint:verdict:1234-abcd",
        text="Referral: Synthetic Party Ltd",
    )
    text = egress_text(record)
    assert text is not None and len(text) <= STATEMENT_LIMIT
    assert "Synthetic Party" not in text and "urn:imprint" not in text
    assert egress_text(item("n", "x", judgment_statement=None)) is None
    assert egress_text(item("w", "   ", judgment_statement="   ")) is None


# -- key resolution ------------------------------------------------------------

def test_missing_env_and_file_makes_no_request():
    transport = FakeTransport()
    result = JevSelector(settings(), transport=transport).select("one", [item("a", "one")])
    assert (result.outcome, result.fallback_reason, result.request_sent) == ("fallback", "missing_key", False)
    assert transport.calls == []


def test_missing_key_file_is_missing_key(tmp_path):
    transport = FakeTransport()
    result = JevSelector(settings(api_key_file=str(tmp_path / "absent")), transport=transport).select(
        "one", [item("a", "one")],
    )
    assert result.fallback_reason == "missing_key"
    assert transport.calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_unsafe_key_file_permissions_fall_back(tmp_path, mode):
    key_file = tmp_path / "key"
    key_file.write_text("synthetic-file-key\n")
    os.chmod(key_file, mode)
    transport = FakeTransport()
    result = JevSelector(settings(api_key_file=str(key_file)), transport=transport).select("one", [item("a", "one")])
    assert result.fallback_reason == "key_file_unsafe"
    assert transport.calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks and modes")
def test_symlinked_or_irregular_key_file_falls_back(tmp_path):
    real = tmp_path / "real-key"
    real.write_text("synthetic-file-key")
    os.chmod(real, 0o600)
    link = tmp_path / "link-key"
    link.symlink_to(real)
    for target in (link, tmp_path):
        transport = FakeTransport()
        result = JevSelector(settings(api_key_file=str(target)), transport=transport).select("one", [item("a", "one")])
        assert result.fallback_reason == "key_file_unsafe"
        assert transport.calls == []


def _private_key_file(tmp_path: Path, value: str) -> Path:
    key_file = tmp_path / "key"
    key_file.write_text(value)
    if os.name != "nt":
        os.chmod(key_file, 0o600)
    return key_file


def test_private_key_file_is_used_and_stripped(tmp_path):
    key_file = _private_key_file(tmp_path, "  synthetic-file-key \n")
    transport = FakeTransport()
    result = JevSelector(settings(api_key_file=str(key_file)), transport=transport).select("one", [item("a", "one")])
    assert result.outcome == "picked"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer synthetic-file-key"


def test_env_key_beats_key_file(tmp_path, monkeypatch):
    key_file = _private_key_file(tmp_path, "synthetic-file-key")
    keyed(monkeypatch, "synthetic-env-key")
    transport = FakeTransport()
    JevSelector(settings(api_key_file=str(key_file)), transport=transport).select("one", [item("a", "one")])
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer synthetic-env-key"


def test_empty_or_header_unsafe_key_is_missing(tmp_path, monkeypatch):
    key_file = _private_key_file(tmp_path, "   \n")
    transport = FakeTransport()
    assert JevSelector(settings(api_key_file=str(key_file)), transport=transport).select(
        "one", [item("a", "one")],
    ).fallback_reason == "missing_key"
    keyed(monkeypatch, "bad key\r\nX-Injected: 1")
    assert JevSelector(settings(), transport=transport).select("one", [item("a", "one")]).fallback_reason == "missing_key"
    assert transport.calls == []


# -- request shape -------------------------------------------------------------

def test_request_body_and_headers_follow_the_documented_contract(monkeypatch):
    keyed(monkeypatch)
    transport = FakeTransport()
    records = [item(f"urn:imprint:verdict:{index}", f"Judgment number {index} about releases") for index in range(3)]
    result = JevSelector(settings(timeout_seconds=2.5), transport=transport).select("releases " * 5000, records)
    assert result.outcome == "picked"
    call = transport.calls[0]
    assert call["endpoint"] == "https://selector.example.invalid/v1/systemone"
    assert set(call["body"]) == {"model", "state", "questions"}
    assert call["body"]["model"] == "jev-test"
    assert len(call["body"]["state"]) == STATE_LIMIT
    assert set(call["body"]["questions"]) == {"c0", "c1", "c2"}
    for question in call["body"]["questions"].values():
        assert set(question) == {"type", "instructions"}
        assert question["type"] == "noul"
        assert question["instructions"].startswith(QUESTION_PREFIX)
        assert len(question["instructions"]) - len(QUESTION_PREFIX) <= STATEMENT_LIMIT
        assert "urn:imprint" not in question["instructions"]
    assert call["headers"]["Authorization"] == "Bearer synthetic-test-key"
    assert call["headers"]["Content-Type"] == "application/json"
    assert 0 < call["timeout"] <= 2.5
    assert {record.record_id for record, _ in result.picks} == {record.record_id for record in records}
    assert result.model_echoed == "jev-test-echo"
    assert result.usage == {"input_tokens": 12, "output_tokens": 3}
    assert result.candidates_sent == 3


def test_prefilter_keep_bounds_questions(monkeypatch):
    keyed(monkeypatch)
    transport = FakeTransport()
    records = [item(f"r{index:03d}", f"statement {index}") for index in range(40)]
    JevSelector(settings(prefilter_keep=7), transport=transport).select("statement", records)
    assert len(transport.calls[0]["body"]["questions"]) == 7


def test_records_without_egress_text_are_never_sent(monkeypatch):
    keyed(monkeypatch)
    transport = FakeTransport()
    result = JevSelector(settings(), transport=transport).select(
        "one", [item("a", "Price: 4200 USD", judgment_statement=None)],
    )
    assert result.fallback_reason == "no_candidates"
    assert transport.calls == []


# -- response validation -------------------------------------------------------

def test_scores_floor_top_and_unknown_names(monkeypatch):
    keyed(monkeypatch)
    records = [item(name, f"write a safe release {name}") for name in "abcd"]
    scores = {"c0": 0.9, "c1": 0.4, "c2": 0.8, "c3": 0.7}

    def raw():
        answers = {name: {"type": "noul", "noul": score} for name, score in scores.items()}
        answers["unknown"] = {"type": "noul", "noul": 0.99}
        answers["urn:imprint:verdict:a"] = {"type": "noul", "noul": 0.99}
        return json.dumps({"model": "jev", "answers": answers}).encode()

    transport = FakeTransport(raw=raw())
    # Prefilter order is a/b/c/d (equal scores, id tie-break), so c0..c3 map to a..d.
    result = JevSelector(settings(top=2, score_floor=0.55), transport=transport).select("zzz", records)
    assert [(x.record_id, score) for x, score in result.picks] == [("a", 0.9), ("c", 0.8)]
    assert result.outcome == "picked"


def test_all_answers_below_floor_is_no_picks(monkeypatch):
    keyed(monkeypatch)
    transport = FakeTransport(lambda _: 0.2)
    result = JevSelector(settings(score_floor=0.55), transport=transport).select("one", [item("a", "one")])
    assert (result.outcome, result.picks, result.fallback_reason) == ("no_picks", (), None)


@pytest.mark.parametrize("answer", [
    {"type": "noul", "noul": True},
    {"type": "noul", "noul": -0.1},
    {"type": "noul", "noul": 1.5},
    {"type": "noul", "noul": "0.9"},
    {"type": "noul", "noul": None},
    {"type": "noul", "noul": 10 ** 400},
    {"noul": 0.9},
    {"type": "score", "noul": 0.9},
    0.9,
    [0.9],
])
def test_malformed_scores_are_rejected(monkeypatch, answer):
    keyed(monkeypatch)
    transport = FakeTransport(raw=json.dumps({"answers": {"c0": answer}}).encode())
    result = JevSelector(settings(), transport=transport).select("one", [item("a", "one")])
    assert (result.outcome, result.fallback_reason) == ("fallback", "bad_response")


def test_non_finite_scores_are_rejected(monkeypatch):
    keyed(monkeypatch)
    transport = FakeTransport(raw=b'{"answers": {"c0": {"type": "noul", "noul": NaN}}}')
    assert JevSelector(settings(), transport=transport).select("one", [item("a", "one")]).fallback_reason == "bad_response"


def test_one_malformed_answer_does_not_discard_valid_ones(monkeypatch):
    keyed(monkeypatch)
    raw = json.dumps({"answers": {"c0": {"type": "noul", "noul": "x"}, "c1": {"type": "noul", "noul": 0.8}}}).encode()
    result = JevSelector(settings(), transport=FakeTransport(raw=raw)).select("one", [item("a", "one"), item("b", "one")])
    assert [x.record_id for x, _ in result.picks] == ["b"]


def test_an_overflowing_score_is_dropped_without_discarding_valid_ones(monkeypatch):
    keyed(monkeypatch)
    raw = json.dumps({"answers": {
        "c0": {"type": "noul", "noul": 0.9}, "c1": {"type": "noul", "noul": 10 ** 400},
    }}).encode()
    result = JevSelector(settings(), transport=FakeTransport(raw=raw)).select("one", [item("a", "one"), item("b", "one")])
    assert (result.outcome, [x.record_id for x, _ in result.picks]) == ("picked", ["a"])


@pytest.mark.parametrize("raw", [
    b"not json",
    b"\xff\xfe",
    b"[]",
    b'"answers"',
    b'{"answers": []}',
    b'{"answers": {}}',
    b'{"answers": {"unknown": {"type": "noul", "noul": 0.9}}}',
    b'{"scores": {"c0": {"type": "noul", "noul": 0.9}}}',
])
def test_bad_responses_fall_back(monkeypatch, raw):
    keyed(monkeypatch)
    result = JevSelector(settings(), transport=FakeTransport(raw=raw)).select("one", [item("a", "one")])
    assert (result.outcome, result.fallback_reason, result.request_sent) == ("fallback", "bad_response", True)


def test_unexpected_echo_fields_are_not_recorded(monkeypatch):
    keyed(monkeypatch)
    raw = json.dumps({
        "model": "model with spaces and prompt text",
        "answers": {"c0": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": True, "output_tokens": -1, "extra": 5},
    }).encode()
    result = JevSelector(settings(), transport=FakeTransport(raw=raw)).select("one", [item("a", "one")])
    assert result.outcome == "picked"
    assert result.model_echoed is None
    assert result.usage is None


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://selector.example.invalid", 401, "Unauthorized", {}, None),
    urllib.error.HTTPError("https://selector.example.invalid", 429, "Too Many Requests", {}, None),
    urllib.error.HTTPError("https://selector.example.invalid", 503, "Unavailable", {}, None),
    urllib.error.URLError(ConnectionRefusedError("refused")),
    ConnectionResetError("reset"),
])
def test_http_and_network_errors_fall_back(monkeypatch, error):
    keyed(monkeypatch)
    result = JevSelector(settings(), transport=FakeTransport(error=error)).select("one", [item("a", "one")])
    assert (result.outcome, result.fallback_reason) == ("fallback", "http_error")


@pytest.mark.parametrize("error", [TimeoutError(), socket.timeout(), urllib.error.URLError(TimeoutError())])
def test_transport_timeouts_fall_back(monkeypatch, error):
    keyed(monkeypatch)
    result = JevSelector(settings(), transport=FakeTransport(error=error)).select("one", [item("a", "one")])
    assert result.fallback_reason == "timeout"


def test_unexpected_exception_falls_back(monkeypatch):
    keyed(monkeypatch)
    result = JevSelector(settings(), transport=FakeTransport(error=RuntimeError("boom"))).select("one", [item("a", "one")])
    assert result.fallback_reason == "exception"


def test_total_deadline_returns_promptly_on_a_slow_endpoint(monkeypatch):
    keyed(monkeypatch)
    transport = FakeTransport(delay=3.0)
    started = time.monotonic()
    result = JevSelector(settings(timeout_seconds=0.3), transport=transport).select("one", [item("a", "one")])
    elapsed = time.monotonic() - started
    assert (result.outcome, result.fallback_reason, result.request_sent) == ("fallback", "timeout", True)
    assert elapsed < 1.5
    assert 250 <= result.elapsed_ms < 1500
    workers = [thread for thread in threading.enumerate() if thread.name == "imprint-jev-request"]
    assert workers and all(thread.daemon for thread in workers)


def test_deadline_covers_key_resolution_and_prefilter(monkeypatch):
    keyed(monkeypatch)
    ticks = iter([0.0, 10.0, 10.0, 10.0, 10.0])
    transport = FakeTransport()
    result = JevSelector(settings(timeout_seconds=3), transport=transport, clock=lambda: next(ticks)).select(
        "one", [item("a", "one")],
    )
    assert result.fallback_reason == "timeout"
    assert transport.calls == []


def test_redirects_are_refused():
    handlers = [
        handler for handler in jev._opener().handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]
    assert len(handlers) == 1
    request = urllib.request.Request("https://selector.example.invalid", data=b"{}", method="POST")
    for code in (301, 302, 303, 307, 308):
        assert handlers[0].redirect_request(request, None, code, "Redirect", {}, "https://elsewhere.invalid") is None


def test_deterministic_hooks_do_not_import_the_selector(tmp_path):
    """Real deterministic hooks, in a fresh interpreter, never load the
    selector module. (The store itself may load urllib.request: Python
    3.14's pathlib uses it for file URIs.)"""
    import subprocess
    import sys
    import textwrap

    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True,
        "domains": [{
            "domain_id": "research", "public_label": "Research",
            "safe_paths": ["Projects/Research"], "keywords": ["sources"], "frozen": False,
        }],
    }), encoding="utf-8")
    code = textwrap.dedent("""
        import io, json, socket, sys

        def blocked(*args, **kwargs):
            raise OSError("outbound network is disabled for the test suite")

        socket.create_connection = socket.getaddrinfo = socket.socket.connect = blocked
        import imprint.cli as cli

        for action, event in (
            ("session-start", {"session_id": "s1", "source": "startup"}),
            ("user-prompt-submit", {"session_id": "s1", "prompt": "hello"}),
            ("user-prompt-submit", {"session_id": "s1", "cwd": "Projects/Research/Now", "prompt": "review sources"}),
            ("session-start", {"session_id": "s1", "source": "compact"}),
        ):
            sys.stdin = io.StringIO(json.dumps(event))
            assert cli.main(["--config", sys.argv[1], "hook", action]) == 0
        print("selector-imported", "imprint.retrieve.jev" in sys.modules, file=sys.stderr)
    """)
    # The default (deterministic) config decides, not the suite's override.
    env = {name: value for name, value in os.environ.items() if name != "IMPRINT_SELECTOR"}
    result = subprocess.run(
        [sys.executable, "-c", code, str(config)], capture_output=True, text=True, env=env, check=True,
    )
    assert result.stdout.count('"hook_schema_version"') == 4
    assert "selector-imported False" in result.stderr


# -- mode resolution and config --------------------------------------------------

@pytest.mark.parametrize("override,configured,expected", [
    (None, "deterministic", "deterministic"),
    (None, "jev", "jev"),
    (None, "shadow", "shadow"),
    ("off", "jev", "deterministic"),
    ("deterministic", "shadow", "deterministic"),
    (" OFF ", "jev", "deterministic"),
    ("jev", "deterministic", "deterministic"),
    ("shadow", "deterministic", "deterministic"),
    ("jev", "jev", "jev"),
    ("shadow", "jev", "jev"),
    ("", "jev", "jev"),
    ("  ", "shadow", "shadow"),
    # Any other value is read as "off": a kill switch must fail safe.
    ("0", "jev", "deterministic"),
    ("false", "jev", "deterministic"),
    ("no", "shadow", "deterministic"),
    ("none", "jev", "deterministic"),
    ("disabled", "jev", "deterministic"),
    ("garbage", "jev", "deterministic"),
])
def test_environment_can_only_turn_the_selector_off(monkeypatch, override, configured, expected):
    if override is None:
        monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    else:
        monkeypatch.setenv("IMPRINT_SELECTOR", override)
    assert effective_mode({"mode": configured}) == expected
    assert effective_mode(None) == "deterministic"


def _write_config(tmp_path, selector, **extra):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_version": "3.0.0", "selector": selector, **extra}), encoding="utf-8")
    return path


def test_selector_defaults_are_deterministic_and_offline(tmp_path):
    loaded = load_config(tmp_path / "missing.json")
    assert loaded["selector"]["mode"] == "deterministic"
    assert loaded["selector"] == DEFAULTS["selector"]
    assert loaded["selector"] is not DEFAULTS["selector"]
    jev_defaults = loaded["selector"]["jev"]
    assert jev_defaults["endpoint"] == "https://api.typesafe.ai/v1/systemone"
    assert jev_defaults["model"] == "jev-1.13.0"
    assert jev_defaults["api_key_env"] == "TYPESAFE_API_KEY"
    assert jev_defaults["api_key_file"] is None
    assert set(jev_defaults["node_types"]) == {"Verdict", "Principle", "Belief", "Value", "Rule", "Pattern"}
    assert jev_defaults["short_prompt_context"] == {"enabled": False, "below_chars": 200, "max_chars": 1500}


def test_selectable_types_are_judgment_types_only():
    from imprint.retrieve.store_source import _JUDGMENT_TYPES, _STATEMENT_FIELDS

    assert SELECTOR_NODE_TYPES <= _JUDGMENT_TYPES
    assert SELECTOR_NODE_TYPES == set(_STATEMENT_FIELDS)
    assert not SELECTOR_NODE_TYPES & {
        "Price", "Purchase", "Refund", "Result", "Observation", "Outcome", "ConsentGrant",
        "SelfModelAssertion", "ChosenFuture", "DefaultFuture", "Aim", "TradeOff", "IngestedItem",
    }


def test_partial_selector_config_merges_nested_defaults(tmp_path):
    path = _write_config(tmp_path, {"mode": "jev", "jev": {"top": 5, "short_prompt_context": {"enabled": True}}})
    loaded = load_config(path)
    assert loaded["selector"]["mode"] == "jev"
    assert loaded["selector"]["jev"]["top"] == 5
    assert loaded["selector"]["jev"]["score_floor"] == 0.55
    assert loaded["selector"]["jev"]["short_prompt_context"] == {"enabled": True, "below_chars": 200, "max_chars": 1500}
    assert DEFAULTS["selector"]["jev"]["top"] == 20
    assert DEFAULTS["selector"]["jev"]["short_prompt_context"]["enabled"] is False


def test_openrouter_route_is_configuration_only(tmp_path):
    path = _write_config(tmp_path, {"mode": "jev", "jev": {
        "endpoint": "https://openrouter.ai/api/alpha/decisions", "model": "typesafe/jev-1.13",
        "api_key_env": "OPENROUTER_API_KEY", "api_key_file": "~/.config/jev/openrouter-key",
    }})
    loaded = load_config(path)["selector"]["jev"]
    assert loaded["model"] == "typesafe/jev-1.13"
    assert loaded["api_key_file"] == "~/.config/jev/openrouter-key"


@pytest.mark.parametrize("selector,extra", [
    ({"mode": "fast"}, {}),
    ({"mode": "jev", "extra": 1}, {}),
    ({"mode": "jev", "jev": {"unknown": 1}}, {}),
    ({"mode": "jev", "jev": {"top": 0}}, {}),
    ({"mode": "jev", "jev": {"top": 151}}, {}),
    ({"mode": "jev", "jev": {"top": True}}, {}),
    ({"mode": "jev", "jev": {"score_floor": 1.5}}, {}),
    ({"mode": "jev", "jev": {"score_floor": False}}, {}),
    ({"mode": "jev", "jev": {"timeout_seconds": 0}}, {}),
    ({"mode": "jev", "jev": {"timeout_seconds": 31}}, {}),
    ({"mode": "jev", "jev": {"timeout_seconds": True}}, {}),
    ({"mode": "jev", "jev": {"prefilter_keep": 0}}, {}),
    ({"mode": "jev", "jev": {"prefilter_keep": 10001}}, {}),
    ({"mode": "jev", "jev": {"endpoint": " "}}, {}),
    ({"mode": "jev", "jev": {"endpoint": "http://api.typesafe.ai/v1/systemone"}}, {}),
    ({"mode": "jev", "jev": {"endpoint": "https://user:secret@api.typesafe.ai/v1/systemone"}}, {}),
    ({"mode": "jev", "jev": {"endpoint": "https:///v1/systemone"}}, {}),
    ({"mode": "jev", "jev": {"endpoint": "https://api.typesafe.ai:notaport/v1"}}, {}),
    ({"mode": "jev", "jev": {"model": ""}}, {}),
    ({"mode": "jev", "jev": {"api_key_env": "BAD-NAME"}}, {}),
    ({"mode": "jev", "jev": {"api_key_file": "relative/key"}}, {}),
    ({"mode": "jev", "jev": {"api_key_file": 7}}, {}),
    ({"mode": "jev", "jev": {"budget_bytes": 1024}}, {}),
    ({"mode": "jev", "jev": {"budget_bytes": True}}, {}),
    ({"mode": "jev", "jev": {"budget_bytes": 65536}}, {}),
    ({"mode": "jev", "jev": {"node_types": []}}, {}),
    ({"mode": "jev", "jev": {"node_types": ["Price"]}}, {}),
    ({"mode": "jev", "jev": {"node_types": ["Verdict", "Verdict"]}}, {}),
    ({"mode": "jev", "jev": {"exclude_domains": ["Not Safe"]}}, {}),
    ({"mode": "jev", "jev": {"short_prompt_context": {"enabled": "yes"}}}, {}),
    ({"mode": "jev", "jev": {"short_prompt_context": {"below_chars": 0}}}, {}),
    ({"mode": "jev", "jev": {"short_prompt_context": {"max_chars": 4001}}}, {}),
    ({"mode": "jev", "jev": {"short_prompt_context": {"other": 1}}}, {}),
    ({"mode": "jev", "jev": {"log_decisions": 1}}, {}),
    ("jev", {}),
    (None, {}),
])
def test_selector_config_rejects_invalid_values(tmp_path, selector, extra):
    with pytest.raises(ValidationError):
        load_config(_write_config(tmp_path, selector, **extra))


def test_higher_selector_budget_requires_explicit_opt_in(tmp_path):
    path = _write_config(tmp_path, {"mode": "jev", "jev": {"budget_bytes": 65536}}, allow_higher_budget=True)
    assert load_config(path)["selector"]["jev"]["budget_bytes"] == 65536


def test_default_selector_block_round_trips(tmp_path):
    path = _write_config(tmp_path, copy.deepcopy(DEFAULTS["selector"]))
    assert load_config(path)["selector"] == DEFAULTS["selector"]
