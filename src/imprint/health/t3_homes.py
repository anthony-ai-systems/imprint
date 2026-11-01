"""Which of T3's Claude homes reach this store, read from T3's own settings.

Hooks run only in a home that registers them, and a home that points them at
another data root fills another store. Neither leaves a trace here, because a
home that never runs a hook writes nothing. T3 lists every home it launches, so
this asks T3 rather than a new setting. It names homes by T3's instance id, and
reports no path and no setting's value.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from imprint.capture.t3_origin import t3_settings_path
from imprint.config import config_path, parse_config, resolved_operator_root
from imprint.paths import home_directory

CLAUDE_DRIVER = "claudeAgent"
# These two mirror tools/install/manage_hooks.py, which runs without this package.
HOOK_MARKER = "imprint-local-managed-hook"
HOOK_ENTRIES = {"SessionStart": 2, "UserPromptSubmit": 1, "Stop": 1}
_SETTINGS_BYTES = 4 * 1024 * 1024
# T3 refuses a settings file with any other instance id.
_INSTANCE_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}")
# What this process has and a hook does not: the launcher exports IMPRINT_CONFIG
# for `imprint` alone, and a session-start health check inherits its own home's.
_NOT_INHERITED = ("IMPRINT_CONFIG", "IMPRINT_DATA_ROOT")


def _read_object(path: Path) -> tuple[str, dict[str, Any] | None]:
    """Read a JSON object as read, missing or unreadable. Never raises, never blocks.

    Decoded first, because json.loads takes a byte order mark and UTF-16 from bytes,
    and a config in either fails every hook that reads it as text.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > _SETTINGS_BYTES:
                return "unreadable", None
            value = json.loads(handle.read(_SETTINGS_BYTES).decode("utf-8"))
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, RecursionError):
        return "unreadable", None
    return ("read", value) if isinstance(value, dict) else ("unreadable", None)


def _claude_instances(configured: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return T3's Claude instances, with the default one it adds if none is stored."""
    instances = {
        name: instance for name, instance in configured.items()
        if isinstance(instance, dict) and instance.get("driver") == CLAUDE_DRIVER
    }
    if CLAUDE_DRIVER not in configured:
        instances[CLAUDE_DRIVER] = {"driver": CLAUDE_DRIVER}
    return instances


def _project_enabled(settings: Mapping[str, Any]) -> set[str]:
    """Return the instances a project turns on, which T3 runs even if disabled overall."""
    overrides = settings.get("projectSettingsOverrides")
    enabled = set()
    for project in overrides.values() if isinstance(overrides, dict) else ():
        instances = project.get("providerInstances") if isinstance(project, dict) else None
        for name, override in instances.items() if isinstance(instances, dict) else ():
            if isinstance(override, dict) and override.get("enabled") is True:
                enabled.add(name)
    return enabled


def _instance_config(instance: Mapping[str, Any]) -> Mapping[str, Any]:
    config = instance.get("config")
    return config if isinstance(config, dict) else {}


def _instance_environment(instance: Mapping[str, Any]) -> dict[str, str]:
    items = instance.get("environment")
    return {
        item["name"]: item["value"] for item in (items if isinstance(items, list) else ())
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str)
    }


def _home_path(config: Mapping[str, Any], environment: Mapping[str, str]) -> Path | None:
    """Resolve a home as T3 does, or None if T3 could not use it.

    T3 expands `~` in homePath and in an instance's CLAUDE_CONFIG_DIR with the
    server's home, not the instance's. With neither set, T3 passes the instance's
    HOME on and Claude finds its home there. The T3 server's own environment cannot
    be seen from here, so an instance that sets none of these is taken to be the
    default home.
    """
    chosen = config.get("homePath", "")
    if not isinstance(chosen, str):
        return None
    chosen = chosen.strip() or environment.get("CLAUDE_CONFIG_DIR", "").strip()
    try:
        path = Path(chosen).expanduser() if chosen else home_directory(environment) / ".claude"
        return path.resolve() if path.is_absolute() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _owned(entry: Any) -> bool:
    commands = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(commands, list) and any(
        isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", "")) for hook in commands
    )


def _hook_state(settings: Mapping[str, Any]) -> str:
    hooks = settings.get("hooks") or {}
    if not isinstance(hooks, dict):
        return "unreadable"
    counts = {}
    for event in HOOK_ENTRIES:
        entries = hooks.get(event)
        counts[event] = sum(_owned(entry) for entry in entries) if isinstance(entries, list) else 0
    if not any(counts.values()):
        return "none"
    return "complete" if all(counts[event] >= need for event, need in HOOK_ENTRIES.items()) else "partial"


def _data_root(root: Path, overrides: Mapping[str, str]) -> str:
    """Compare the store a hook would open under these overrides with this one.

    The T3 server's environment cannot be seen from here, so this process's stands
    in for it. The path can come from any home and this runs inside a session-start
    hook, so the file is read with the same guard as settings, and nothing about one
    home can cost the report on the others.
    """
    environ = {name: value for name, value in os.environ.items() if name not in _NOT_INHERITED}
    environ.update(overrides)
    try:
        path = config_path(environ)
        # A relative path is relative to a hook's folder, which cannot be known here.
        state, loaded = _read_object(path) if path.is_absolute() else ("unreadable", None)
        if state == "unreadable":
            return "unknown"
        resolved = resolved_operator_root(parse_config(loaded or {}), environ)
        return "same" if resolved == root.resolve() else "different"
    except Exception:
        return "unknown"


def _worst(values: Iterable[str]) -> str:
    seen = set(values)
    return next((value for value in ("different", "unknown") if value in seen), "same")


def _home_row(home: Path | str, members: list[tuple[str, bool, dict[str, str]]], root: Path) -> dict[str, Any]:
    """Describe one home and every instance T3 launches on it."""
    state, settings = _read_object(home / "settings.json") if isinstance(home, Path) else ("unreadable", None)
    row: dict[str, Any] = {
        "instances": [name for name, _, _ in members],
        "enabled": any(enabled for _, enabled, _ in members),
        "hooks": "unreadable",
        "data_root": "unknown",
    }
    if state == "unreadable":
        return row
    settings = settings or {}
    declared = settings.get("env")
    declared = {
        name: value for name, value in (declared if isinstance(declared, dict) else {}).items()
        if isinstance(value, str)
    }
    try:
        row["hooks"] = _hook_state(settings)
    except Exception:  # A command too deep to read, say. The row stays unreadable.
        pass
    # A disabled instance launches nothing, so it speaks only for a home where none
    # can run. Claude Code applies a home's env over the one it was started with.
    running = [member for member in members if member[1]] or members
    row["data_root"] = _worst(_data_root(root, {**environment, **declared}) for _, _, environment in running)
    return row


def _summary(state: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count only what can run: a disabled instance launches nothing."""
    enabled = [row for row in rows if row["enabled"]]
    return {
        "state": state,
        "homes": rows,
        "homes_missing_hooks": sum(row["hooks"] in {"none", "partial"} for row in enabled),
        "homes_different_data_root": sum(row["data_root"] == "different" for row in enabled),
        "homes_unreadable": sum(row["hooks"] == "unreadable" or row["data_root"] == "unknown" for row in enabled),
        "evidence": "t3_settings_provider_instances_plus_home_settings",
    }


def _read_homes(root: Path) -> dict[str, Any]:
    path = t3_settings_path()
    state, settings = ("unreadable", None) if path is None else _read_object(path)
    if settings is None:
        return _summary(f"settings_{state}", [])
    configured = settings.get("providerInstances", {})
    if not isinstance(configured, dict) or any(_INSTANCE_ID.fullmatch(name) is None for name in configured):
        return _summary("settings_unreadable", [])
    project_enabled = _project_enabled(settings)
    grouped: dict[Path | str, list[tuple[str, bool, dict[str, str]]]] = {}
    for name, instance in sorted(_claude_instances(configured).items()):
        config = _instance_config(instance)
        environment = _instance_environment(instance)
        enabled = name in project_enabled or (
            instance.get("enabled") is not False and config.get("enabled") is not False
        )
        grouped.setdefault(_home_path(config, environment) or name, []).append((name, enabled, environment))
    return _summary("read", [_home_row(home, members, root) for home, members in grouped.items()])


def t3_home_report(root: Path) -> dict[str, Any]:
    """Report every Claude home T3 can launch. Health never fails on this."""
    try:
        return _read_homes(Path(root))
    except Exception:
        return _summary("settings_unreadable", [])
