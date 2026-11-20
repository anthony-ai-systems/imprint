"""The typed-decision package stays off the live path: no hook and no part of the
selector imports it. Every record here is synthetic, and every request goes to a fake.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[2]
HOOKS = ROOT / "hooks"
KEY_ENV = "ISOLATION_TEST_KEY"

# Loaded by every Python process the hooks start, through PYTHONPATH: the bridge
# itself, the CLI child it runs, and the delivery commit after it. It cuts the
# network, gives the selector a fake Jev and one synthetic candidate when asked, and
# on exit appends which decide modules the process loaded.
SHIM = textwrap.dedent('''
    import atexit, json, os, socket, sys

    def _blocked(*args, **kwargs):
        raise OSError("outbound network is disabled for the test suite")

    socket.create_connection = socket.getaddrinfo = socket.socket.connect = _blocked

    if os.environ.get("DECIDE_ISOLATION_FAKE_JEV") == "1":
        import imprint.retrieve as _retrieve
        import imprint.retrieve.jev as _jev
        from imprint.retrieve.models import RetrievalRecord as _Record

        def _fake(endpoint, body, headers, timeout):
            questions = json.loads(body)["questions"]
            return json.dumps({"answers": {n: {"type": "noul", "noul": 0.9} for n in questions}}).encode()

        class _Source:
            def __init__(self, store):
                pass

            def retrieval_candidates(self, snapshot_id, **kwargs):
                return (_Record(
                    record_id="urn:synthetic:judgment:0001", text="Review synthetic sources twice.",
                    section="general", provenance_status="captured", authority_tier="captured_judgment",
                    evidence_ids=("urn:synthetic:evidence:1",), case_ids=("urn:synthetic:case:1",),
                    provenance_complete=True, ontology_type="Verdict",
                    judgment_statement="Review synthetic sources twice.",
                ),)

        _jev.http_post = _fake
        _retrieve.StoreRetrievalSource = _Source

    def _record():
        loaded = sorted(n for n in sys.modules if n == "imprint.decide" or n.startswith("imprint.decide."))
        with open(os.environ["DECIDE_ISOLATION_RECORD"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"process": sys.argv[1:3], "decide": loaded}) + "\\n")

    atexit.register(_record)
''')


def run_hooks(tmp_path: Path, mode: str) -> tuple[list[dict], list[str]]:
    """Every hook script, run as the host runs it, under one selector mode."""
    work = tmp_path / mode
    shim = work / "shim"
    shim.mkdir(parents=True)
    (shim / "sitecustomize.py").write_text(SHIM, encoding="utf-8")
    config = work / "config.json"
    config.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(work / "data"), "operator_slug": "test",
        "node_id": "primary", "compiler": True,
        "domains": [{"domain_id": "research", "public_label": "Research", "safe_paths": ["Projects/Research"]}],
        "selector": {"mode": mode, "jev": {"api_key_env": KEY_ENV}},
    }), encoding="utf-8")
    transcript = work / "transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "user", "promptSource": "typed", "message": {"content": "No, keep the synthetic label."},
    }) + "\n", encoding="utf-8")
    record = work / "imports.jsonl"
    env = {name: value for name, value in os.environ.items()
           if name not in {"IMPRINT_SELECTOR", "IMPRINT_DECIDE", "IMPRINT_CAPTURE_ORIGIN"}}
    env.update({
        "HOME": str(work / "home"), "XDG_CONFIG_HOME": str(work / "home" / ".config"),
        "IMPRINT_CONFIG": str(config), KEY_ENV: "synthetic-test-key",
        "PYTHONPATH": os.pathsep.join([str(shim), str(ROOT / "src")]),
        "DECIDE_ISOLATION_RECORD": str(record), "DECIDE_ISOLATION_FAKE_JEV": "1" if mode == "jev" else "0",
    })
    outputs = []
    for script, event in (
        ("session_start.py", {"session_id": "s1", "source": "startup"}),
        ("user_prompt_submit.py", {"session_id": "s1", "prompt": "review the synthetic sources"}),
        ("user_prompt_submit.py", {"session_id": "s1", "cwd": "Projects/Research/Now", "prompt": "sources"}),
        ("stop_capture.py", {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)}),
        ("health_check.py", {"session_id": "s1"}),
        ("session_start.py", {"session_id": "s1", "source": "compact"}),
    ):
        result = subprocess.run(
            [sys.executable, str(HOOKS / script)], input=json.dumps(event),
            capture_output=True, text=True, env=env, check=False, timeout=60,
        )
        assert result.returncode == 0, (script, result.stdout, result.stderr)
        outputs.append(result.stdout)
    lines = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    return lines, outputs


def test_the_real_hooks_never_import_the_decide_package(tmp_path):
    """Each hook script through the real bridge, deterministic and jev, in fresh
    processes: no process any hook starts loads the package."""
    for mode in ("deterministic", "jev"):
        processes, outputs = run_hooks(tmp_path, mode)
        assert all(json.loads(output)["hook_schema_version"] == "1.0.0" for output in outputs)
        # The bridge processes and the CLI children they ran all reported in.
        children = {tuple(line["process"]) for line in processes}
        for action in ("session-start", "user-prompt-submit", "stop-capture", "health-check"):
            assert ("hook", action) in children, (mode, action)
        assert [line for line in processes if line["decide"]] == [], mode
        if mode == "jev":
            assert any('"selector_method": "jev"' in output for output in outputs)


def _strings(nodes: list[ast.expr]) -> list[str]:
    """The text constants among ``nodes``, and inside list or tuple literals among them."""
    found = []
    for node in nodes:
        items = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
        found.extend(item.value for item in items if isinstance(item, ast.Constant) and isinstance(item.value, str))
    return found


def decide_import_nodes(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """Every module an import statement or an import call names that has a ``decide``
    part, with the node that names it: ``import imprint.decide``, ``from imprint import
    decide``, ``from .decide.x import y``, ``importlib.import_module("imprint.decide")``,
    ``import_module(name="imprint.decide")``, ``__import__("imprint", fromlist=["decide"])``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [f"{node.module or ''}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) in {
            "import_module", "__import__",
        }:
            names = _strings([*node.args, *(keyword.value for keyword in node.keywords)])
        else:
            continue
        found.extend((name, node) for name in names if "decide" in name.split("."))
    return found


def decide_imports(path: Path) -> list[str]:
    return [name for name, _ in decide_import_nodes(ast.parse(path.read_text(encoding="utf-8")))]


def _selector_eval_branch(node: ast.AST) -> bool:
    test = getattr(node, "test", None)
    return (
        isinstance(node, ast.If) and isinstance(test, ast.Compare) and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq) and ast.unparse(test.left) == "args.command"
        and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value == "selector-eval"
    )


def stray_cli_decide_imports(source: str) -> list[str]:
    """Decide imports in the CLI other than the one lazy import inside the offline
    ``selector-eval`` branch, wherever they sit."""
    tree = ast.parse(source)
    allowed = {
        id(inner) for branch in ast.walk(tree) if _selector_eval_branch(branch)
        for statement in branch.body for inner in ast.walk(statement)
    }
    return [
        name for name, node in decide_import_nodes(tree)
        if id(node) not in allowed or name != "decide.evaluate.run_selector_eval"
    ]


def test_the_check_finds_each_way_of_importing(tmp_path):
    probe = tmp_path / "probe.py"
    for line in ("import imprint.decide", "from imprint import decide", "from .decide.client import ask",
                 "import importlib; importlib.import_module('imprint.decide')", "__import__('imprint.decide')",
                 "import importlib; importlib.import_module(name='imprint.decide')",
                 "import importlib; importlib.import_module('.evaluate', package='imprint.decide')",
                 "__import__('imprint', fromlist=['decide'])"):
        probe.write_text(line + "\n", encoding="utf-8")
        assert decide_imports(probe), line
    probe.write_text("from imprint.config import decide_setting, DECIDE_PURPOSES\n", encoding="utf-8")
    assert decide_imports(probe) == []


def test_the_cli_check_allows_the_import_only_inside_the_offline_command():
    lazy = textwrap.dedent('''
        def main(args):
            if args.command == "selector-eval":
                from .decide.evaluate import run_selector_eval
                return run_selector_eval
    ''')
    assert stray_cli_decide_imports(lazy) == []
    for stray in (
        "from .decide.evaluate import run_selector_eval\n" + lazy,
        lazy.replace('"selector-eval"', '"hook"'),
        lazy + "    from .decide.evaluate import run_selector_eval\n",
        lazy.replace("from .decide.evaluate import run_selector_eval", "from .decide.client import ask"),
    ):
        assert stray_cli_decide_imports(stray), stray


def test_the_live_path_names_no_decide_import():
    source = ROOT / "src" / "imprint"
    hooks = sorted(HOOKS.glob("*.py"))
    assert {path.name for path in hooks} >= {
        "_bridge.py", "health_check.py", "session_start.py", "stop_capture.py", "user_prompt_submit.py",
    }
    live = [*(source / "retrieve").glob("*.py"), *(source / "health").glob("*.py"), *hooks,
            source / "hook_routes.py", source / "selection_notice.py", source / "config.py"]
    assert all(path.is_file() for path in live)
    for path in live:
        assert not decide_imports(path), path.name
    # At most one lazy import, inside the offline selector-eval command only.
    assert stray_cli_decide_imports((source / "cli.py").read_text(encoding="utf-8")) == []
