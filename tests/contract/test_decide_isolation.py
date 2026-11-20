"""The typed-decision package stays off the live path: no hook and no part of the
selector imports it. Every record here is synthetic, and every request goes to a fake.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[2]
KEY_ENV = "ISOLATION_TEST_KEY"


# -- neither the hooks nor the selector load the module ----------------------------

def test_hooks_and_the_selector_never_import_the_decide_package(tmp_path):
    """Real hooks in a fresh interpreter, deterministic and jev, with a fake Jev, and
    a direct import of the selector, health and route modules: none loads it."""
    code = textwrap.dedent("""
        import io, json, socket, sys

        def blocked(*args, **kwargs):
            raise OSError("outbound network is disabled for the test suite")

        socket.create_connection = socket.getaddrinfo = socket.socket.connect = blocked
        import imprint.cli as cli
        import imprint.health, imprint.hook_routes, imprint.retrieve, imprint.retrieve.jev as jev
        from imprint.retrieve.models import RetrievalRecord

        def fake(endpoint, body, headers, timeout):
            questions = json.loads(body)["questions"]
            return json.dumps({"answers": {n: {"type": "noul", "noul": 0.9} for n in questions}}).encode()

        class Source:
            def __init__(self, store):
                pass

            def retrieval_candidates(self, snapshot_id, **kwargs):
                return (RetrievalRecord(
                    record_id="urn:synthetic:judgment:0001", text="Review synthetic sources twice.",
                    section="general", provenance_status="captured", authority_tier="captured_judgment",
                    evidence_ids=("urn:synthetic:evidence:1",), case_ids=("urn:synthetic:case:1",),
                    provenance_complete=True, ontology_type="Verdict",
                    judgment_statement="Review synthetic sources twice.",
                ),)

        jev.http_post = fake
        imprint.retrieve.StoreRetrievalSource = Source
        for config in sys.argv[1:]:
            for action, event in (
                ("session-start", {"session_id": "s1", "source": "startup"}),
                ("user-prompt-submit", {"session_id": "s1", "prompt": "review the synthetic sources"}),
                ("user-prompt-submit", {"session_id": "s1", "cwd": "Projects/Research/Now", "prompt": "sources"}),
                ("stop-capture", {"session_id": "s1"}),
                ("health-check", {"session_id": "s1"}),
                ("session-start", {"session_id": "s1", "source": "compact"}),
            ):
                sys.stdin = io.StringIO(json.dumps(event))
                cli.main(["--config", config, "hook", action])
        loaded = sorted(name for name in sys.modules if name.startswith("imprint.decide"))
        print("decide-imported", loaded, "jev-imported", "imprint.retrieve.jev" in sys.modules, file=sys.stderr)
    """)
    configs = []
    for mode in ("deterministic", "jev"):
        path = tmp_path / f"{mode}.json"
        path.write_text(json.dumps({
            "config_version": "3.0.0", "data_root": str(tmp_path / mode), "operator_slug": "test",
            "node_id": "primary", "compiler": True,
            "domains": [{"domain_id": "research", "public_label": "Research", "safe_paths": ["Projects/Research"]}],
            "selector": {"mode": mode, "jev": {"api_key_env": KEY_ENV}},
        }), encoding="utf-8")
        configs.append(str(path))
    env = {name: value for name, value in os.environ.items() if name not in {"IMPRINT_SELECTOR", "IMPRINT_DECIDE"}}
    env[KEY_ENV] = "synthetic-test-key"
    result = subprocess.run(
        [sys.executable, "-c", code, *configs], capture_output=True, text=True, env=env, check=True,
    )
    assert result.stdout.count('"hook_schema_version"') >= 10
    assert '"selector_method": "jev"' in result.stdout
    assert "decide-imported [] jev-imported True" in result.stderr


def decide_imports(path: Path) -> list[str]:
    pattern = re.compile(r"^\s*(?:from|import)\s+[\w.]*\bdecide\b")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if pattern.match(line)]


def test_the_live_path_names_no_decide_import():
    source = ROOT / "src" / "imprint"
    live = [*(source / "retrieve").glob("*.py"), *(source / "health").glob("*.py"), *(source / "hooks").glob("*.py"),
            source / "hook_routes.py", source / "selection_notice.py", source / "config.py"]
    for path in live:
        assert not decide_imports(path), path.name
    # At most one lazy import, inside the offline selector-eval command only.
    assert set(decide_imports(source / "cli.py")) <= {"from .decide.evaluate import run_selector_eval"}
