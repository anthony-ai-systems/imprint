"""Replay one fixed run of the hooks and CLI, so two builds compare byte for byte.

Run it as a script with ``PYTHONPATH`` on the build under test. It prints one
JSON object, ``{scenario: sha256}``, or the text itself with ``--full``. Clocks,
ids and Jev are stand-ins, and no socket can connect. Every step here runs with
no scope row and no scope extension. ``ratify apply`` and the legacy import are
left out, because both write a row.

Three flags are for this branch's own tests, never for main's golden. ``--rows``
writes one scope row on every current record after the seed, straight into the
table, so no id or clock read moves, most of them with a subject. ``--stamps``
puts a scope stamp carrying ``said_in``, ``channel`` and ``carrier`` on the
seed's CLI capture, so those words sit on its event. ``--bodies`` adds each Jev
request body, as sent, as one more scenario.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import pkgutil
import random
import re
import secrets
import socket
import sqlite3
import subprocess  # noqa: F401  held before ``time`` is patched, as ``threading`` is
import sys
import tempfile
import threading  # noqa: F401
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

EPOCH = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
KEY_ENV = "SCOPE_IDENTITY_TEST_KEY"
ACTOR = "synthetic-operator"
# Words that must never reach a thread or Jev: they sit on the CLI capture's event only.
EVENT_ONLY_MARKS = {"said_in": "said-in-mark-aardvark", "channel": "channel-mark-narwhal",
                    "carrier": "carrier-mark-pangolin"}


class Clock:
    """One second a read, so a build that reads the clock once more shows up too."""

    seconds = 0

    @classmethod
    def read(cls) -> datetime:
        cls.seconds += 1
        return EPOCH + timedelta(seconds=cls.seconds)


class SteppingDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.read()


def _refused(*args, **kwargs):
    raise OSError("the identity driver has no network")


ids = random.Random(20261101)
uuid.uuid4 = lambda: uuid.UUID(int=ids.getrandbits(128), version=4)
secrets.token_hex = lambda nbytes=32: ids.randbytes(nbytes).hex()
time.time = lambda: (EPOCH + timedelta(seconds=Clock.seconds)).timestamp()
time.monotonic = lambda: 1000.0
socket.socket.connect = _refused
socket.create_connection = _refused
socket.getaddrinfo = _refused

import imprint  # noqa: E402

for info in pkgutil.walk_packages(imprint.__path__, "imprint."):
    if not info.name.endswith("__main__"):
        importlib.import_module(info.name)
for name, module in list(sys.modules.items()):
    if name.startswith("imprint") and getattr(module, "datetime", None) is datetime:
        module.datetime = SteppingDatetime

from imprint.capture.schema import build_capture_envelope  # noqa: E402
from imprint.cli import main as imprint_main  # noqa: E402
from imprint.config import load_config  # noqa: E402
from imprint.portability.jsonld import TABLES  # noqa: E402
from imprint.retrieve import _snapshot_id, jev, select_prompt_payload  # noqa: E402
from imprint.store import ImprintStore  # noqa: E402


class Transport:
    """Answers the way the service does."""

    def __init__(self):
        self.score, self.error = 0.9, None
        self.bodies: list[str] = []

    def __call__(self, endpoint, body, headers, timeout):
        self.bodies.append(body.decode("utf-8") if isinstance(body, bytes) else body)
        if self.error is not None:
            raise self.error
        questions = json.loads(body)["questions"]
        return json.dumps({
            "model": "jev-test-echo",
            "answers": {name: {"type": "noul", "noul": self.score} for name in questions},
            "usage": {"input_tokens": 40, "output_tokens": 4},
        }).encode("utf-8")


class Replay:
    def __init__(self, work: Path, stamps: bool = False):
        self.work = work
        self.stamps = stamps
        self.data = work / "data"
        self.root = self.data / "synthetic"
        self.transport = Transport()
        self.scenarios: dict[str, str] = {}
        jev.http_post = self.transport
        self.configs = {mode: self._config(mode, self.data) for mode in ("deterministic", "jev", "shadow")}
        self.config = self.configs["deterministic"]

    def _config(self, mode: str, data: Path) -> Path:
        path = self.work / f"config-{mode}-{data.name}.json"
        path.write_text(json.dumps({
            "config_version": "3.0.0", "data_root": str(data), "operator_slug": "synthetic",
            "node_id": "primary", "compiler": True, "context_budget_bytes": 32768,
            "domains": [{"domain_id": "research", "public_label": "Research", "keywords": ["benchmark"]}],
            "selector": {"mode": mode, "jev": {"api_key_env": KEY_ENV}},
        }))
        return path

    def clean(self, text: str) -> str:
        for form in sorted({str(self.work), str(self.work.resolve())}, key=len, reverse=True):
            text = text.replace(form, "<root>")
        return text

    def record(self, name: str, text: str) -> str:
        self.scenarios[name] = self.clean(text)
        return text

    def run(self, name: str, *argv: str, event: dict | None = None, config: Path | None = None) -> str:
        out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        stdin, stdout, stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO("" if event is None else json.dumps(event))
        sys.stdout, sys.stderr = out, err
        try:
            code = imprint_main(["--config", str(config or self.config), *argv])
        except SystemExit as exc:
            code = exc.code
        finally:
            sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
        out.flush(), err.flush()
        printed = out.buffer.getvalue().decode("utf-8")
        self.record(name, f"exit {code}\n{printed}\n--\n{err.buffer.getvalue().decode('utf-8')}")
        return printed

    def hook(self, name: str, action: str, config: Path | None = None, **event) -> dict:
        return json.loads(self.run(name, "hook", action, event=event, config=config))

    def store(self) -> ImprintStore:
        return ImprintStore(self.root / "imprint.db")

    def derived(self, node_type: str, statement: str, evidence: str, operator: str, **payload) -> str:
        return self.store().append_derived_node(
            node_type=node_type, payload={"statement": statement, **payload},
            provenance_status="inferred", authority_tier="inferred_candidate", evidence_ids=[evidence],
            operator_id=operator, valid_from=Clock.read().isoformat().replace("+00:00", "Z"), proposed_by="driver",
        )

    def seed(self) -> dict[str, str]:
        self.hook("stop_capture_feedback", "stop-capture", session_id="seed-1",
                  operator_text="No, report the failed source because omissions change the decision.")
        self.hook("stop_capture_second", "stop-capture", session_id="seed-2",
                  operator_text="No, keep the benchmark table because it is the only proof we have.")
        self.hook("stop_capture_no_feedback", "stop-capture", session_id="seed-3",
                  operator_text="What time is the review?")
        operator = json.loads((self.root / "identity.json").read_text())["operator_id"]
        stamp = {"extensions": {"org.imprint.scope": {"schema_version": "1.0.0", "payload": {
            "scope_kind": "client", "client_id": "a-client", "resolved_by": "driver", **EVENT_ONLY_MARKS,
        }}}} if self.stamps else {}
        envelope = build_capture_envelope(
            operator_id=operator, session_id="urn:imprint:session:00000000-0000-4000-8000-000000000001",
            node_id="primary", case_description="A synthetic draft", raw_operator_text="Prefer the shorter summary.",
            call_type="prefer", capture_mechanism="explicit_cli", captured_by="driver", **stamp,
        )
        event = self.work / "event.json"
        event.write_text(json.dumps(envelope))
        self.run("capture_cli", "capture", "--event", str(event))
        self.run("compile_once", "compile", "--once")
        self.run("compile_again", "compile", "--once")

        store = self.store()
        evidence = store.current_nodes(["Evidence"])[0]["node_id"]
        rule = self.derived("Rule", "Name the source that failed.", evidence, operator)
        belief = self.derived("Belief", "Omissions change decisions.", evidence, operator)
        domain_rule = self.derived("Rule", "Keep the benchmark table.", evidence, operator, domain_id="research")
        self.run("review_list", "review", "list")
        self.run("review_show", "review", "show", rule)
        for name, node in (("rule", rule), ("belief", belief), ("domain_rule", domain_rule)):
            self.run(f"review_ratify_{name}", "review", "ratify", node, "--by", ACTOR)
        return {
            "operator": operator, "evidence": evidence, "rule": rule,
            "verdict": store.current_nodes(["Verdict"])[0]["node_id"],
        }

    def deliveries(self) -> None:
        self.hook("session_start_first", "session-start", session_id="reader", source="startup")
        self.hook("session_start_delivered", "session-start", session_id="reader", source="startup")
        self.hook("session_start_compact", "session-start", session_id="reader", source="compact")
        self.hook("session_start_resume", "session-start", session_id="reader", source="resume")
        self.hook("prompt_hook_skip", "user-prompt-submit", session_id="reader", prompt="How is the day going?")
        self.hook("prompt_hook_domain", "user-prompt-submit", session_id="reader", prompt="Update the benchmark.")
        self.hook("prompt_hook_domain_delivered", "user-prompt-submit", session_id="reader", prompt="Update the benchmark.")
        self.hook("prompt_hook_explicit_domain", "user-prompt-submit", session_id="other", prompt="Go on.",
                  domain_id="research")
        self.run("retrieve", "retrieve", "--session", "cli", "--prompt", "Which source failed?")
        self.run("retrieve_delivered", "retrieve", "--session", "cli", "--prompt", "Which source failed?")
        self.run("retrieve_refresh", "retrieve", "--session", "cli", "--prompt", "Which source failed?", "--refresh")
        self.run("retrieve_domain", "retrieve", "--session", "cli-domain", "--prompt", "benchmark", "--domain", "research")

    def selector(self) -> None:
        turn = {"session_id": "jev-reader", "prompt": "Should the summary say which source failed?"}
        config = self.configs["jev"]
        self.hook("jev_pick", "user-prompt-submit", config=config, **turn)
        self.hook("jev_pick_delivered", "user-prompt-submit", config=config, **turn)
        self.hook("jev_session_start_compact", "session-start", config=config, session_id="jev-reader", source="compact")

        loaded = load_config(config)
        for name in ("jev_prepared", "jev_prepared_replay"):
            decision = select_prompt_payload(
                self.store(), root=self.root, session_id="urn:imprint:session:prepared",
                state="Say which source failed.", explicit_domain=None, budget=32768,
                selector=loaded["selector"], mode="jev",
            )
            self.record(name, json.dumps(decision.response, sort_keys=True))

        self.run("jev_retrieve", "retrieve", "--session", "jev-cli", "--prompt", "Say which source failed.", config=config)
        self.transport.score = 0.1
        self.hook("jev_no_picks", "user-prompt-submit", config=config, session_id="jev-quiet",
                  prompt="Is there anything on the benchmark?")
        self.transport.score, self.transport.error = 0.9, TimeoutError("synthetic timeout")
        self.hook("jev_fallback", "user-prompt-submit", config=config, session_id="jev-failed",
                  prompt="Is there anything on the benchmark?")
        self.transport.error = None
        self.hook("jev_shadow", "user-prompt-submit", config=self.configs["shadow"], session_id="jev-shadow",
                  prompt="Is there anything on the benchmark?")

    def records(self, seeded: dict[str, str]) -> None:
        operator, evidence, verdict = seeded["operator"], seeded["evidence"], seeded["verdict"]
        self.run("history", "history", verdict)
        self.run("verdict_add_reason", "verdict", "add-reason", verdict, "--reason", "It changes the call.", "--by", ACTOR)
        self.run("verdict_reinforce", "verdict", "reinforce", verdict, "--evidence", "Said again.", "--by", ACTOR)
        self.run("history_after_changes", "history", verdict)
        old = self.derived("Rule", "Old wording.", evidence, operator)
        new = self.derived("Rule", "New wording.", evidence, operator)
        self.run("transition_supersede", "transition", "supersede", new, old, "--reason", "Replaced.",
                 "--evidence", evidence, "--by", ACTOR)
        self.run("history_superseded", "history", old)
        doomed = self.derived("Rule", "A rule that goes.", evidence, operator)
        self.run("tombstone", "delete", "tombstone", "--scope", doomed, "--reason", "Withdrawn.")
        self.run("purge_preview", "delete", "purge", "--scope", doomed, "--preview")
        self.run("purge", "delete", "purge", "--scope", doomed, "--confirm", doomed, "--sentinel", "A rule that goes.")

    def reads(self) -> None:
        store = self.store()
        self.record("snapshot_id", _snapshot_id(store))
        self.record("snapshot", json.dumps(store.snapshot(), sort_keys=True))
        exported = self.run("export_jsonld", "export", "--format", "jsonld")
        self.run("export_markdown", "export", "--format", "markdown")
        self.run("health", "health")
        # What the disk says today: free space, file ages, and which receipt has the newest mtime.
        self.scenarios["health"] = re.sub(
            r'("(?:[a-z_]*age_seconds|disk_free_bytes|selected_bytes)": )-?\d+(\.\d+)?', r"\1<disk>",
            self.scenarios["health"],
        )
        self.run("migrate_verify", "migrate", "verify")

        document = self.work / "export.json"
        document.write_text(exported)
        fresh = self._config("deterministic", self.work / "fresh")
        self.run("import_dry_run", "import", "--format", "jsonld", "--input", str(document), "--dry-run", config=fresh)
        self.run("import", "import", "--format", "jsonld", "--input", str(document), config=fresh)
        self.run("export_after_import", "export", "--format", "jsonld", config=fresh)

        with sqlite3.connect(self.root / "imprint.db") as conn:
            self.record("meta_versions", json.dumps(sorted(conn.execute("SELECT key,value FROM meta").fetchall())))
            # Main's own tables, row for row. ``record_scope`` is the one schema difference.
            self.record("store_rows", json.dumps({
                table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in TABLES
            }, sort_keys=True))
        written = {}
        for path in sorted(self.data.rglob("*")):
            if path.is_file() and ".db" not in path.name and not path.name.startswith(".compiler"):
                written[str(path.relative_to(self.data))] = self.clean(path.read_text("utf-8", "replace"))
        self.record("files_on_disk", json.dumps(written, sort_keys=True))

    def rows(self) -> None:
        """One row on every current record, of each kind in turn. A stamped capture's own row is replaced."""
        kinds = (
            ("client", "a-client", "bfcm-2026", "specific", 1, "marketing.zebracopy"),
            ("central", None, None, None, None, "engineering.quokka"),
            ("core_owner", None, None, None, None, "work.wombat"),
            ("held", None, None, None, None, None),
            ("unresolved", None, None, None, None, "design.okapi"),
            ("client", "other-client", None, "general", None, None),
        )
        with sqlite3.connect(self.root / "imprint.db") as conn:
            nodes = [row[0] for row in conn.execute("SELECT node_id FROM nodes ORDER BY node_id")]
            for index, node in enumerate(nodes):
                kind, client, project, delivery, rank, subject = kinds[index % len(kinds)]
                conn.execute(
                    "INSERT OR REPLACE INTO record_scope (node_id, scope_kind, client_id, project_id, delivery,"
                    " delivery_rank, subject, overrides_json, resolved_by, event_id, stamped_at)"
                    " VALUES (?,?,?,?,?,?,?,'[]','driver','driver',?)",
                    (node, kind, client, project, delivery, rank, subject, EPOCH.isoformat()),
                )


def replay(work: Path, rows: bool = False, bodies: bool = False, stamps: bool = False) -> dict[str, str]:
    os.environ.update({
        "HOME": str(work / "home"), "XDG_CONFIG_HOME": str(work / "xdg-config"),
        "XDG_DATA_HOME": str(work / "xdg-data"), "XDG_STATE_HOME": str(work / "xdg-state"),
        "IMPRINT_CONFIG": str(work / "unused-config.json"), "IMPRINT_DATA_ROOT": str(work / "unused-data"),
        "T3CODE_HOME": str(work / "t3"), KEY_ENV: "synthetic-test-key",
    })
    for name in ("IMPRINT_SELECTOR", "IMPRINT_DEFER_DELIVERY_COMMIT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(name, None)
    run = Replay(work, stamps=stamps)
    seeded = run.seed()
    if rows:
        run.rows()
    run.deliveries()
    run.selector()
    run.records(seeded)
    run.reads()
    if bodies:
        run.scenarios["jev_request_bodies"] = run.clean(json.dumps(run.transport.bodies))
    return run.scenarios


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="imprint-scope-identity-") as folder:
        scenarios = replay(Path(folder), rows="--rows" in sys.argv, bodies="--bodies" in sys.argv,
                           stamps="--stamps" in sys.argv)
    if "--full" in sys.argv:
        print(json.dumps(scenarios, indent=1, sort_keys=True))
    else:
        print(json.dumps({
            name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in sorted(scenarios.items())
        }, indent=1))
