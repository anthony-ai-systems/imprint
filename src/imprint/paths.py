"""Portable paths with explicit override and sync-root refusal."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Mapping

from .errors import SafetyError

SYNC_MARKERS = ("Dropbox", "OneDrive", "CloudStorage", "Google Drive")
HOME_VARIABLE = "USERPROFILE" if os.name == "nt" else "HOME"


def home_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Return the home these variables name. ``Path.home()`` reads only this process's."""
    home = None if environ is None else environ.get(HOME_VARIABLE)
    return Path(home) if home else Path.home()


def expand_home(path: Path | str, environ: Mapping[str, str] | None = None) -> Path:
    """Expand a leading ``~`` as a process given these variables would."""
    path = Path(path)
    if path.parts[:1] == ("~",):
        return home_directory(environ).joinpath(*path.parts[1:])
    return path.expanduser()


def default_data_root(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("IMPRINT_DATA_ROOT")
    if override:
        root = expand_home(override, environ)
    elif platform.system() == "Windows":
        base = values.get("LOCALAPPDATA")
        if not base:
            raise SafetyError("LOCALAPPDATA is required on Windows")
        root = Path(base) / "Imprint"
    else:
        root = Path(values.get("XDG_DATA_HOME", home_directory(environ) / ".local" / "share")) / "imprint"
    return validate_data_root(root, environ=environ)


def validate_data_root(
    path: Path, *, allow_sync: bool = False, environ: Mapping[str, str] | None = None,
) -> Path:
    path = expand_home(path, environ)
    if not path.is_absolute():
        raise SafetyError("Imprint data root must be absolute")
    resolved = path.resolve(strict=False)
    if resolved == Path(resolved.anchor) or resolved == home_directory(environ).resolve():
        raise SafetyError("Refusing root or home as Imprint data root")
    if not allow_sync and any(marker.lower() in str(resolved).lower() for marker in SYNC_MARKERS):
        raise SafetyError("Cloud-sync roots are unsupported for the canonical database")
    return resolved


def operator_root(operator_id: str, base: Path | None = None) -> Path:
    safe = operator_id.strip().lower()
    if not safe or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in safe):
        raise SafetyError("operator_id must use lowercase letters, digits, and hyphens")
    return (base or default_data_root()) / safe

