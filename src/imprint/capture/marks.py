"""Content-free once-capture markers keyed by session and source transcript entry."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from imprint.permissions import secure_directory, secure_file

ALREADY_CAPTURED_REASON = "operator_turn_already_captured"
MARK_SCHEMA_VERSION = "1.0.0"


def turn_identity(source_entry_id: str | None, operator_text: str) -> str:
    """Name the operator turn by its transcript entry, or by its bytes.

    The transcript entry uuid is stable across Stop hooks over an unchanged
    transcript. Explicit hook events carry no entry, so the utterance itself
    supplies the identity.
    """
    if isinstance(source_entry_id, str) and source_entry_id.strip():
        return f"entry:{source_entry_id.strip()}"
    digest = hashlib.sha256(operator_text.encode("utf-8")).hexdigest()
    return f"text:sha256:{digest}"


class CapturedTurns:
    """Per-session markers recording which operator turns were already spooled.

    Stop fires on every assistant turn, so an unchanged transcript is re-read
    many times with the same trailing operator utterance. A marker is claimed
    atomically by link, so a repeat is a skip rather than a duplicate Verdict.
    Only hashes are stored: the marker never contains operator content.
    """

    def __init__(self, operator_root: Path):
        self.root = Path(operator_root) / "runtime" / "captured-turns"

    def _mark_path(self, session_id: str, turn_id: str) -> Path:
        session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        turn = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:32]
        directory = self.root / session
        secure_directory(directory)
        return directory / f"{turn}.json"

    def claim(self, session_id: str, turn_id: str) -> bool:
        """Return True exactly once per session and turn."""
        final = self._mark_path(session_id, turn_id)
        if final.exists():
            secure_file(final)
            return False
        payload = json.dumps(
            {"mark_schema_version": MARK_SCHEMA_VERSION, "type": "captured_turn"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        fd, temporary_name = tempfile.mkstemp(prefix=".mark-", dir=final.parent)
        temporary = Path(temporary_name)
        os.close(fd)
        try:
            secure_file(temporary)
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            secure_file(temporary)
            try:
                os.link(temporary, final)
            except FileExistsError:
                return False
            secure_file(final)
            return True
        finally:
            temporary.unlink(missing_ok=True)
