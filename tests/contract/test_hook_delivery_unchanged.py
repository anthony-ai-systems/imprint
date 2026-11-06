"""Recording where a turn went changes no byte a hook delivers, on any route or setting.

This file imports nothing the route log added, on purpose: it has to run unchanged
against the merged code before it, commit 0c561dd, which is where its digests came
from. Each digest covers every turn's whole stdout, run directly and through the
bridge, with all retrieval on synthetic records and every request to a fake.

If a later item changes delivery on purpose, it re-pins these and says why.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import imprint.cli as cli
import imprint.retrieve as retrieval
from imprint.config import DEFAULTS
from imprint.retrieve import jev
from imprint.retrieve.models import RetrievalRecord

ROOT = Path(__file__).parents[2]
KEY_ENV = "JEV_TEST_KEY"
PROMPT_SWITCH = "org.imprint.short_lines.prompt"
SESSION_START_SWITCH = "org.imprint.short_lines.session_start"
LONG = "Some background about the project and how it came about. " * 12
TURNS = (
    ("session-start", {"session_id": "s", "source": "startup"}),
    ("session-start", {"session_id": "s", "source": "startup"}),
    ("user-prompt-submit", {"session_id": "s", "prompt": "the failed source"}),
    ("user-prompt-submit", {"session_id": "s", "prompt": "the failed source", "domain_id": "research"}),
    ("user-prompt-submit", {"session_id": "s", "prompt": "the failed source", "domain_id": "research"}),
    ("user-prompt-submit", {"session_id": "s", "prompt": "   ", "domain_id": "research"}),
    ("session-start", {"session_id": "s", "source": "compact"}),
)


def record(index: int, text: str, section: str = "general", **overrides) -> RetrievalRecord:
    values = dict(
        record_id=f"urn:synthetic:judgment:{index:04d}", text=text, section=section,
        provenance_status="captured", authority_tier="captured_judgment",
        evidence_ids=("urn:synthetic:evidence:1",), case_ids=("urn:synthetic:case:1",),
        case_summaries=("Synthetic case summary",), source_receipt_ids=("urn:synthetic:receipt:1",),
        provenance_complete=True, ontology_type="Verdict", ontology_path=("judgment", "Verdict"),
        judgment_statement=text, valid_from="2026-01-01T00:00:00Z",
    )
    values.update(overrides)
    return RetrievalRecord(**values)


RECORDS = [
    record(1, "Believe the failed source.", "core", ontology_type="Belief", provenance_status="ratified",
           authority_tier="ratified_knowledge", case_ids=(), case_summaries=(), ontology_path=("judgment", "Belief")),
    record(2, "Never use the vendor template."),
    record(3, f"No, that is wrong. {LONG}Keep the label."),
    record(4, "Report the failed source.", "domain", domain_id="research"),
    record(5, f"The headline looks good. {LONG}But the table is wrong.", "domain", domain_id="research"),
    record(7, "A declared offer.", "domain", domain_id="research", ontology_partition="business_declared",
           ontology_type="Offer", judgment_statement=None),
]


class Source:
    def __init__(self, records):
        self.records = records

    def retrieval_candidates(self, snapshot_id, **kwargs):
        return tuple(self.records)


class Transport:
    def __init__(self):
        self.score = 0.9
        self.error: Exception | None = None
        self.raw: bytes | None = None

    def __call__(self, endpoint, body, headers, timeout):
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        questions = json.loads(body)["questions"]
        return json.dumps({"answers": {
            name: {"type": "noul", "noul": self.score - int(name[1:]) / 100} for name in questions
        }}).encode("utf-8")


class InProcess:
    """Stands in for the bridge's child, so a fake transport reaches the real hook."""

    def __init__(self, argv, env, **kwargs):
        self.args, self.env, self.returncode = argv, env, None

    def communicate(self, payload, timeout=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(sys, "stdin", io.StringIO(payload)):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.returncode = cli.main(self.args[3:])
        return stdout.getvalue(), stderr.getvalue()


def bridge_module():
    spec = importlib.util.spec_from_file_location("imprint_test_delivery_bridge", ROOT / "hooks" / "_bridge.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selector(mode: str, **jev_values) -> dict:
    value = json.loads(json.dumps(DEFAULTS["selector"]))
    value["mode"] = mode
    value["jev"]["api_key_env"] = KEY_ENV
    value["jev"].update(jev_values)
    return value


SETTINGS = {
    "deterministic": {"selector": selector("deterministic")},
    "jev": {"selector": selector("jev")},
    "jev_decision_log_off": {"selector": selector("jev", log_decisions=False)},
    "jev_tight_budget": {"selector": selector("jev", budget_bytes=4096)},
    "jev_short_lines": {"selector": selector("jev"), PROMPT_SWITCH: True, SESSION_START_SWITCH: True},
    "shadow": {"selector": selector("shadow")},
    "deterministic_short_lines": {
        "selector": selector("deterministic"), PROMPT_SWITCH: True, SESSION_START_SWITCH: True,
    },
}


def _low_scores(transport, monkeypatch):
    transport.score = 0.05


def _timeout(transport, monkeypatch):
    transport.error = TimeoutError("synthetic timeout")


def _http_error(transport, monkeypatch):
    transport.error = ConnectionError("synthetic refusal")


def _bad_response(transport, monkeypatch):
    transport.raw = b"not json"


def _env_unset(transport, monkeypatch):
    monkeypatch.delenv(KEY_ENV)


def _empty_store(transport, monkeypatch):
    monkeypatch.setattr(retrieval, "StoreRetrievalSource", lambda store: Source([]))


def _render_fails(transport, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(retrieval, "_jev_response", boom)


def _scratch(transport, monkeypatch):
    monkeypatch.setattr(cli, "_t3_scratch_thread", lambda config, session: True)


def _selector_off(transport, monkeypatch):
    monkeypatch.setenv("IMPRINT_SELECTOR", "off")


def _pick_that_cannot_fit(transport, monkeypatch):
    wide = record(8, "A rule with a long trail.", evidence_ids=tuple(f"urn:synthetic:evidence:{n}" for n in range(500)))
    monkeypatch.setattr(retrieval, "StoreRetrievalSource", lambda store: Source([wide]))


CONDITIONS = {
    "answers": lambda transport, monkeypatch: None,
    "low_scores": _low_scores,
    "timeout": _timeout,
    "http_error": _http_error,
    "bad_response": _bad_response,
    "env_unset": _env_unset,
    "empty_store": _empty_store,
    "render_fails": _render_fails,
    "scratch": _scratch,
    "selector_off": _selector_off,
    "pick_that_cannot_fit": _pick_that_cannot_fit,
}

EXPECTED = {
    "deterministic/answers": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/low_scores": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/timeout": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/http_error": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/bad_response": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/env_unset": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/empty_store": "9ad75bc70b1c1ba6e3db7f2fe897681941b11958eb82b8a5f48d41c494bca8b4",
    "deterministic/render_fails": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/scratch": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/selector_off": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "deterministic/pick_that_cannot_fit": "9ad75bc70b1c1ba6e3db7f2fe897681941b11958eb82b8a5f48d41c494bca8b4",
    "jev/answers": "9173a07b5b179985925fea4e6c97131695b52cb702e635b2f4ca14ee0dec2a0c",
    "jev/low_scores": "73f537be1617a9bac35e624e75827fda1405c3eece64d88c6070b5f433eb9eb4",
    "jev/timeout": "3d5459c9c4b8be6f7ac38fcfb0bc6d4f3837f6fc7ecebd967b6c3fab5faf64c2",
    "jev/http_error": "20d1146a1f4b0ce9fbbc3e250b6c9dd090dfbaddd0439906177a53a1690cb9c4",
    "jev/bad_response": "e662cdce29a1c5909f442cfe4130f567e9dc0b529c64daf04107da192df9869f",
    "jev/env_unset": "2caebcac6cb9879f0ce9ba98549eb1ddfb388a567d850431d297e16128a2f8a7",
    "jev/empty_store": "f5cb2213ac2b9a643520ffc8db38ea642307f4c39e5f1248b4a199bdf76a5cb6",
    "jev/render_fails": "f6f9b20036fa0914c8c0f6c9c8266bdd5179c647df38e5260297e38707fdab72",
    "jev/scratch": "34a6eee9df2b281c4499417e3e0f79e6b10e38f583030e5ea9fd680906742b90",
    "jev/selector_off": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "jev/pick_that_cannot_fit": "cfc2175d905ea0a6f805a3513accae3a935dfb33f7d1eb7af5a3efbbc9b657d2",
    "jev_decision_log_off/answers": "9173a07b5b179985925fea4e6c97131695b52cb702e635b2f4ca14ee0dec2a0c",
    "jev_decision_log_off/low_scores": "73f537be1617a9bac35e624e75827fda1405c3eece64d88c6070b5f433eb9eb4",
    "jev_decision_log_off/timeout": "3d5459c9c4b8be6f7ac38fcfb0bc6d4f3837f6fc7ecebd967b6c3fab5faf64c2",
    "jev_decision_log_off/http_error": "20d1146a1f4b0ce9fbbc3e250b6c9dd090dfbaddd0439906177a53a1690cb9c4",
    "jev_decision_log_off/bad_response": "e662cdce29a1c5909f442cfe4130f567e9dc0b529c64daf04107da192df9869f",
    "jev_decision_log_off/env_unset": "2caebcac6cb9879f0ce9ba98549eb1ddfb388a567d850431d297e16128a2f8a7",
    "jev_decision_log_off/empty_store": "f5cb2213ac2b9a643520ffc8db38ea642307f4c39e5f1248b4a199bdf76a5cb6",
    "jev_decision_log_off/render_fails": "f6f9b20036fa0914c8c0f6c9c8266bdd5179c647df38e5260297e38707fdab72",
    "jev_decision_log_off/scratch": "34a6eee9df2b281c4499417e3e0f79e6b10e38f583030e5ea9fd680906742b90",
    "jev_decision_log_off/selector_off": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "jev_decision_log_off/pick_that_cannot_fit": "cfc2175d905ea0a6f805a3513accae3a935dfb33f7d1eb7af5a3efbbc9b657d2",
    "jev_tight_budget/answers": "9173a07b5b179985925fea4e6c97131695b52cb702e635b2f4ca14ee0dec2a0c",
    "jev_tight_budget/low_scores": "73f537be1617a9bac35e624e75827fda1405c3eece64d88c6070b5f433eb9eb4",
    "jev_tight_budget/timeout": "3d5459c9c4b8be6f7ac38fcfb0bc6d4f3837f6fc7ecebd967b6c3fab5faf64c2",
    "jev_tight_budget/http_error": "20d1146a1f4b0ce9fbbc3e250b6c9dd090dfbaddd0439906177a53a1690cb9c4",
    "jev_tight_budget/bad_response": "e662cdce29a1c5909f442cfe4130f567e9dc0b529c64daf04107da192df9869f",
    "jev_tight_budget/env_unset": "2caebcac6cb9879f0ce9ba98549eb1ddfb388a567d850431d297e16128a2f8a7",
    "jev_tight_budget/empty_store": "f5cb2213ac2b9a643520ffc8db38ea642307f4c39e5f1248b4a199bdf76a5cb6",
    "jev_tight_budget/render_fails": "f6f9b20036fa0914c8c0f6c9c8266bdd5179c647df38e5260297e38707fdab72",
    "jev_tight_budget/scratch": "34a6eee9df2b281c4499417e3e0f79e6b10e38f583030e5ea9fd680906742b90",
    "jev_tight_budget/selector_off": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "jev_tight_budget/pick_that_cannot_fit": "cfc2175d905ea0a6f805a3513accae3a935dfb33f7d1eb7af5a3efbbc9b657d2",
    "jev_short_lines/answers": "13b3f7589faeef6670a8dc8b74f6b24ea3647903d9b0983204ab5fb38bcd5d3f",
    "jev_short_lines/low_scores": "be941de231576914c405d24da388d3a0313578cd09ad7fa8a43e2839c81a9f8f",
    "jev_short_lines/timeout": "dd5aa85408d042de55705615993327ed5d5a66a15cc052d1fad64baadfb74b14",
    "jev_short_lines/http_error": "624fe68340f656a8ea6c71b9ff3fde8145463dfa1faadf36e253453332146422",
    "jev_short_lines/bad_response": "928edb1cca324dd6b31a5a6cd3182b5c1bf9c0e49d77039b96a800e1459c8c0b",
    "jev_short_lines/env_unset": "20b4762acb28176855031785c05f0c882563c4d3474b469547a91f9c791f82b0",
    "jev_short_lines/empty_store": "f5cb2213ac2b9a643520ffc8db38ea642307f4c39e5f1248b4a199bdf76a5cb6",
    "jev_short_lines/render_fails": "e44248e67d839df5e7c384f7566f3568afa07c71b337144ce0b55d320cb77fc5",
    "jev_short_lines/scratch": "637abd01ab49091228713d0359c6efc0f607da4cb627778cd81592cca54036df",
    "jev_short_lines/selector_off": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "jev_short_lines/pick_that_cannot_fit": "d0eb7f4fbef01f2d3ce7be6383b81215f12d3a6865ae730b623eaa5e6acc2c12",
    "shadow/answers": "fc5548e8e4631c3833b8f6921c5dbd83a8b37dde04a6610e6a8f70eeb906f054",
    "shadow/low_scores": "eb0201c92982cac0f9c740f37577168c760d4c828786a939801db145c422a705",
    "shadow/timeout": "405e134cbc9c7ef9cb84f77d2a5b967fd68bcd286f12160dbd601b2f57b796b2",
    "shadow/http_error": "8ad12860ea0b35d48fdc06aea47fca803a8c5cae9f7986e6c612f493d63c6ab4",
    "shadow/bad_response": "aeefd90eb4d86015a3f1761fa324b36ae43bb42c7bdbe788d1b32cfc6738cbc4",
    "shadow/env_unset": "fc460841b0bd8aa78e36d28ff5fd93045663169cb29e069f54e47f7f817552ff",
    "shadow/empty_store": "9333d86db694d7a943895a09c158ee6a6f6882411a3b0d641338c55a5e358dfd",
    "shadow/render_fails": "fc5548e8e4631c3833b8f6921c5dbd83a8b37dde04a6610e6a8f70eeb906f054",
    "shadow/scratch": "34a6eee9df2b281c4499417e3e0f79e6b10e38f583030e5ea9fd680906742b90",
    "shadow/selector_off": "f9cf9f7a23a1d3dd2aee9acee3a6d190911bfea5baf104fab258d615a3670106",
    "shadow/pick_that_cannot_fit": "530a3cffde2d504646f09f2cb6c267f14ffdf8b0e67c7c55747d6909f3e71cb7",
    "deterministic_short_lines/answers": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/low_scores": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/timeout": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/http_error": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/bad_response": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/env_unset": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/empty_store": "9ad75bc70b1c1ba6e3db7f2fe897681941b11958eb82b8a5f48d41c494bca8b4",
    "deterministic_short_lines/render_fails": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/scratch": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/selector_off": "25c88f2e99d7f7601520e992802b4f6c9a65e2706f2aa273b0055b0044fca01a",
    "deterministic_short_lines/pick_that_cannot_fit": "af4bbc3495af2157460a242e80889d57dccc41629f5685cb97a9eb21c4e94b0c",
}


def run_turns(tmp_path, name, setting, run) -> str:
    folder = tmp_path / name
    folder.mkdir()
    config = folder / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(folder / "data"), "operator_slug": "test", "node_id": "primary",
        "compiler": True, "context_budget_bytes": 8192,
        "domains": [{"domain_id": "research", "public_label": "Research"}], **setting,
    }))
    return "".join(run(config, action, event) for action, event in TURNS)


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("setting", SETTINGS)
def test_no_route_delivers_a_different_byte(tmp_path, monkeypatch, capsys, setting, condition):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")
    monkeypatch.setattr(retrieval, "StoreRetrievalSource", lambda store: Source(RECORDS))
    transport = Transport()
    monkeypatch.setattr(jev, "http_post", transport)
    CONDITIONS[condition](transport, monkeypatch)
    bridge = bridge_module()
    monkeypatch.setattr(bridge.subprocess, "Popen", InProcess)

    def direct(config, action, event):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        assert cli.main(["--config", str(config), "hook", action]) == 0
        return capsys.readouterr().out

    def bridged(config, action, event):
        monkeypatch.setenv("IMPRINT_CONFIG", str(config))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        assert bridge.run(action) == 0
        return capsys.readouterr().out

    seen = run_turns(tmp_path, "direct", SETTINGS[setting], direct) + run_turns(tmp_path, "bridged", SETTINGS[setting], bridged)
    value = hashlib.sha256(seen.encode("utf-8")).hexdigest()
    assert value == EXPECTED[f"{setting}/{condition}"]
