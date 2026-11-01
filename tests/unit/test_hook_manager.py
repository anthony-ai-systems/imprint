from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "install" / "manage_hooks.py"
SPEC = importlib.util.spec_from_file_location("imprint_manage_hooks", MODULE_PATH)
assert SPEC and SPEC.loader
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)


def test_registration_is_idempotent_and_preserves_unrelated_hooks(tmp_path):
    settings = {
        "theme": "dark",
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]}],
        },
    }
    python = tmp_path / "Python With Spaces" / "python"
    directory = tmp_path / "Hooks With Spaces"
    once = hooks.mutate(settings, "register", python, directory)
    twice = hooks.mutate(once, "register", python, directory)
    assert twice["theme"] == "dark"
    assert sum("other-tool" in json.dumps(item) for item in twice["hooks"]["Stop"]) == 1
    assert hooks._counts(twice) == {"SessionStart": 2, "UserPromptSubmit": 1, "Stop": 1}


def test_registration_preserves_symlinked_python_path(tmp_path):
    settings_path = tmp_path / "settings.json"
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path(sys.executable).resolve())

    assert hooks.main([
        "register",
        "--settings", str(settings_path),
        "--python", str(venv_python),
        "--hooks-dir", str(tmp_path / "hooks"),
    ]) == 0

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    registered_python = shlex.split(command)[0]
    assert registered_python == str(venv_python)
    assert registered_python != str(venv_python.resolve())


def test_unregistration_removes_only_owned_entries(tmp_path):
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]},
        {"matcher": "", "hooks": [{"type": "command", "command": "run # imprint-local-managed-hook"}]},
    ]}}
    result = hooks.mutate(settings, "unregister", tmp_path / "python", tmp_path / "hooks")
    assert result["hooks"]["Stop"] == [
        {"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]},
    ]


def test_atomic_write_creates_parseable_settings_and_backup(tmp_path):
    path = tmp_path / "Claude Settings" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"existing": true}\n', encoding="utf-8")
    hooks._write(path, {"existing": True, "new": "value"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"existing": True, "new": "value"}
    assert list(path.parent.glob("settings.json.imprint-backup-*"))


DOCUMENTS = ("README.md", "docs/configuration.md", "docs/troubleshooting.md", "docs/contracts/capture-origin.md")


def test_the_docs_name_the_packaged_hook_commands_and_never_the_installer():
    root = MODULE_PATH.parents[2]
    for name in DOCUMENTS:
        assert "install.sh --settings" not in (root / name).read_text(encoding="utf-8"), name
    guide = (root / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert "umask 077\n" in guide
    for action in ("register", "unregister", "status"):
        assert f'"$INSTALL_ROOT/tools/manage_hooks.py" {action} \\\n' in guide
