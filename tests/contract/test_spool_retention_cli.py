from __future__ import annotations

import json
import os

from imprint.cli import main
from imprint.compiler import compile_spools, write_envelope
from imprint.store import ImprintStore


def test_spool_prune_cli_uses_configured_producer_and_retention(tmp_path, capsys, capture_envelope):
    data = tmp_path / "data"
    root = data / "test"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test",
        "node_id": capture_envelope["node_id"], "compiler": True,
        "spool_retention_days": 36500,
    }))
    path = write_envelope(root, capture_envelope)
    assert compile_spools(root, ImprintStore(root / "imprint.db"), compiler_authorized=True)["captured"] == 1
    assert main(["--config", str(config), "spool", "prune"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "deleted": 0, "invalid": 0, "retained": 1, "status": "ok",
        "hook_failures_pruned": 0,
    }
    assert path.exists()


def test_spool_prune_cli_also_prunes_expired_hook_failure_diagnostics(tmp_path, capsys):
    data = tmp_path / "data"
    root = data / "test"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_root": str(data), "operator_slug": "test",
        "node_id": "workstation-a", "compiler": True,
        "spool_retention_days": 30,
    }))
    failures = root / "logs" / "hook-failures"
    failures.mkdir(parents=True)
    expired = failures / "20260101T000000.000000Z-expired.json"
    expired.write_text("{}\n", encoding="utf-8")
    aged = expired.stat().st_mtime - 86400 * 60
    os.utime(expired, (aged, aged))
    fresh = failures / "20260803T000000.000000Z-fresh.json"
    fresh.write_text("{}\n", encoding="utf-8")

    assert main(["--config", str(config), "spool", "prune"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["hook_failures_pruned"] == 1
    assert not expired.exists()
    assert fresh.exists()
