"""Nothing changes for anyone yet. With no rows and no extension, a build prints what main printed.

``fixtures/scope_identity/golden.json`` holds one hash per scenario, made by running
``driver.py`` on ec5fc59. The tests that run ec5fc59 itself need that commit, so a
shallow checkout skips them and keeps the golden and the committed export.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest

from imprint.compiler import write_envelope
from imprint.portability.jsonld import export_jsonld, import_jsonld
from imprint.store import ImprintStore
from imprint.store.scope import SCOPE_EXTENSION


MAIN = "ec5fc59"
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "scope_identity"
GOLDEN = json.loads((FIXTURES / "golden.json").read_text())
# One line per step in the driver. A dropped scenario must not pass quietly.
SCENARIOS = 53
CLI = "import sys; from imprint.cli import main; sys.exit(main(sys.argv[1:]))"


def environment(source: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(source), "PYTHONHASHSEED": "0"}


def replay(source: Path) -> dict[str, str]:
    result = subprocess.run(
        [sys.executable, str(FIXTURES / "driver.py")], env=environment(source),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def different(left: dict[str, str], right: dict[str, str]) -> list[str]:
    return sorted(name for name in set(left) | set(right) if left.get(name) != right.get(name))


def test_every_scenario_is_there():
    assert len(GOLDEN) == SCENARIOS


def test_no_rows_and_no_extension_replay_mains_bytes():
    assert different(replay(SOURCE), GOLDEN) == []


@pytest.fixture(scope="module")
def main_source(tmp_path_factory) -> Path:
    if shutil.which("git") is None or subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{MAIN}^{{commit}}"], capture_output=True,
    ).returncode != 0:
        pytest.skip(f"{MAIN} is not in this checkout")
    folder = tmp_path_factory.mktemp("main")
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", MAIN, "src", "hooks"], capture_output=True, check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                path = folder / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile(member).read())
    return folder / "src"


def on_build(source: Path, config: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", CLI, "--config", str(config), *argv], env=environment(source),
        capture_output=True, text=True, timeout=60,
    )


def printed(source: Path, config: Path, *argv: str) -> dict:
    result = on_build(source, config, *argv)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def configured(tmp_path: Path, operator_id: str, name: str = "data") -> tuple[Path, Path, ImprintStore]:
    data = tmp_path / name
    root = data / "test-operator"
    root.mkdir(parents=True)
    (root / "identity.json").write_text(json.dumps({"identity_schema_version": "1.0.0", "operator_id": operator_id}))
    config = tmp_path / f"{name}.json"
    config.write_text(json.dumps({
        "operator_slug": "test-operator", "data_root": str(data), "compiler": True, "node_id": "workstation-a",
    }))
    return config, root, ImprintStore(root / "imprint.db")


def stamped(envelope: dict, **payload) -> dict:
    value = deepcopy(envelope)
    value["extensions"] = {SCOPE_EXTENSION: {"schema_version": "1.0.0", "payload": {
        "scope_kind": "client", "client_id": "example-client", "resolved_by": "thread_folder", **payload,
    }}}
    return value


def rows_of(store: ImprintStore) -> list[dict]:
    with store.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM record_scope")]


def test_the_golden_is_mains_own_output(main_source):
    assert different(replay(main_source), GOLDEN) == []


def test_main_opens_a_store_with_rows_and_leaves_them(tmp_path, main_source, capture_envelope):
    config, _, store = configured(tmp_path, capture_envelope["operator_id"])
    store.initialize()
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    before = rows_of(store)

    verdict_id = envelope["verdict"]["verdict_id"]
    assert printed(main_source, config, "history", verdict_id)["history"]["node_id"] == verdict_id
    assert printed(main_source, config, "retrieve", "--session", "s", "--prompt", "source")["status"] == "delivered"
    assert "imprint:recordScope" not in printed(main_source, config, "export", "--format", "jsonld")
    assert on_build(main_source, config, "health").returncode in {0, 2}
    assert rows_of(store) == before


def test_a_capture_main_compiles_keeps_its_stamp_on_the_event_and_gets_no_row(tmp_path, main_source, capture_envelope):
    config, root, store = configured(tmp_path, capture_envelope["operator_id"])
    store.initialize()
    envelope = stamped(capture_envelope)
    write_envelope(root, envelope)

    assert printed(main_source, config, "compile", "--once")["captured"] == 1

    assert rows_of(store) == []
    version = store.node_history(envelope["verdict"]["verdict_id"])["versions"][0]
    assert version["event_payload"]["extensions"][SCOPE_EXTENSION]["payload"]["client_id"] == "example-client"


def test_mains_purge_leaves_the_row_and_reports_residue(tmp_path, main_source, capture_envelope):
    config, _, store = configured(tmp_path, capture_envelope["operator_id"])
    store.initialize()
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]

    result = on_build(main_source, config, "delete", "purge", "--scope", verdict_id, "--confirm", verdict_id)

    assert json.loads(result.stdout)["status"] == "purged_with_residue", result.stdout + result.stderr
    assert [row["node_id"] for row in rows_of(store)] == [verdict_id]
    assert "imprint:recordScope" not in export_jsonld(store)


def test_an_export_with_rows_imports_on_main_and_loses_only_the_rows(tmp_path, main_source, capture_envelope):
    _, _, store = configured(tmp_path, capture_envelope["operator_id"])
    store.initialize()
    store.apply_capture(stamped(capture_envelope))
    document = export_jsonld(store)
    assert document["imprint:recordScope"]
    path = tmp_path / "export.json"
    path.write_text(json.dumps(document))

    config, _, _ = configured(tmp_path, capture_envelope["operator_id"], "other")
    imported = printed(main_source, config, "import", "--format", "jsonld", "--input", str(path))

    assert imported["semantic_sha256"] == document["imprint:semanticSha256"]
    again = printed(main_source, config, "export", "--format", "jsonld")
    assert again == {key: value for key, value in document.items() if "ecordScope" not in key}


def test_mains_export_imports_here_and_reads_as_legacy(tmp_path, main_source, capture_envelope):
    config, _, store = configured(tmp_path, capture_envelope["operator_id"])
    store.initialize()
    store.apply_capture(capture_envelope)
    document = printed(main_source, config, "export", "--format", "jsonld")
    _, _, imported = configured(tmp_path, capture_envelope["operator_id"], "other")

    assert import_jsonld(imported, document) == document["imprint:semanticSha256"]

    assert rows_of(imported) == []
    assert export_jsonld(imported) == document


def test_the_committed_main_export_imports_here_and_reads_as_legacy(tmp_path):
    """The same crossing with no commit to run, from a store 830eb7f wrote."""
    document = json.loads((FIXTURES / "main_export.json").read_text())
    imported = ImprintStore(tmp_path / "imprint.db")

    assert import_jsonld(imported, document) == document["imprint:semanticSha256"]

    assert rows_of(imported) == []
    assert export_jsonld(imported) == document
