"""No test may write into the live operator spool.

Running the suite used to drop hook-failure records into the real operator's
data root: bridge tests stub a failed capture, ``_persist_failure`` resolves
``load_config() -> resolved_operator_root()``, and without an override that is
the live install (observed as "boom" records under
``~/.local/share/imprint/<operator>/logs/hook-failures/``). The autouse
``isolated_data_root`` fixture now pins IMPRINT_CONFIG / IMPRINT_DATA_ROOT
into ``tmp_path``; this file exercises exactly the path that used to leak and
proves the record lands in the isolated root while the live spool gains
nothing. The session-scoped ``operator_spool_guard`` additionally fails the
whole run if any other test regresses.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _spool_names(path: Path | None) -> frozenset[str]:
    if path is None or not path.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in path.iterdir())


def _bridge_module():
    spec = importlib.util.spec_from_file_location(
        "imprint_test_spool_isolation_bridge", ROOT / "hooks" / "_bridge.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_resolution_lands_in_isolated_root(isolated_data_root):
    from imprint.config import load_config, resolved_operator_root

    assert Path(os.environ["IMPRINT_DATA_ROOT"]) == isolated_data_root
    assert not Path(os.environ["IMPRINT_CONFIG"]).exists()
    assert resolved_operator_root(load_config()) == isolated_data_root / "default"


def test_stop_capture_failure_record_stays_out_of_live_spool(
    monkeypatch, capsys, isolated_data_root, operator_spool_guard,
):
    live_before = _spool_names(operator_spool_guard.path)
    bridge = _bridge_module()
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": False})))
    monkeypatch.setattr(
        bridge,
        "_graceful_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0] if args else [], 1, "", "boom"),
    )

    assert bridge.run("stop-capture") == 2
    capsys.readouterr()

    records = list((isolated_data_root / "default" / "logs" / "hook-failures").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["stderr"] == "boom"
    assert not _spool_names(operator_spool_guard.path) - live_before
