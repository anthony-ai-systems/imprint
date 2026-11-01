"""Portable paths with explicit override and sync-root refusal."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Mapping

from .errors import SafetyError

SYNC_MARKERS = ("Dropbox", "OneDrive", "CloudStorage", "Google Drive")


def default_data_root(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("IMPRINT_DATA_ROOT")
    if override:
        root = Path(override).expanduser()
    elif platform.system() == "Windows":
        base = values.get("LOCALAPPDATA")
        if not base:
            raise SafetyError("LOCALAPPDATA is required on Windows")
        root = Path(base) / "Imprint"
    else:
        root = Path(values.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "imprint"
    return validate_data_root(root)


def validate_data_root(path: Path, *, allow_sync: bool = False) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise SafetyError("Imprint data root must be absolute")
    resolved = path.resolve(strict=False)
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise SafetyError("Refusing root or home as Imprint data root")
    if not allow_sync and any(marker.lower() in str(resolved).lower() for marker in SYNC_MARKERS):
        raise SafetyError("Cloud-sync roots are unsupported for the canonical database")
    return resolved


def operator_root(operator_id: str, base: Path | None = None) -> Path:
    safe = operator_id.strip().lower()
    if not safe or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in safe):
        raise SafetyError("operator_id must use lowercase letters, digits, and hyphens")
    return (base or default_data_root()) / safe

