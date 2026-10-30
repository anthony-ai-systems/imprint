"""Atomic node-local once-delivery receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from imprint.permissions import secure_directory, secure_file

_SAFE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Bounds for the cross-snapshot session dedup scan (newest files first).
DELIVERED_SCAN_FILES = 256
DELIVERED_SCAN_BYTES = 1024 * 1024


class DeliveryReceipts:
    def __init__(self, session_root: Path):
        self.root = Path(session_root)

    @staticmethod
    def _safe(value: str, label: str) -> str:
        if not _SAFE.fullmatch(value):
            raise ValueError(f"unsafe {label}")
        return value

    def _claim(self, session_id: str, snapshot_id: str, kind: str, domain_id: str | None) -> bool:
        session = self._safe(session_id, "session id")
        snapshot = hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()[:24]
        suffix = "session-start" if domain_id is None else f"domain-{self._safe(domain_id, 'domain id')}"
        directory = self.root / session
        secure_directory(self.root)
        secure_directory(directory)
        final = directory / f"{snapshot}-{suffix}.json"
        receipt = json.dumps(
            {"receipt_schema_version": "1.0.0", "snapshot_ref": snapshot, "type": kind},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
        temporary_path = Path(temporary)
        os.close(fd)
        try:
            secure_file(temporary_path)
            with temporary_path.open("wb") as handle:
                handle.write(receipt)
                handle.flush()
                os.fsync(handle.fileno())
            secure_file(temporary_path)
            try:
                os.link(temporary, final)
                secure_file(final)
                return True
            except FileExistsError:
                secure_file(final)
                return False
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _paths(
        self, session_id: str, snapshot_id: str, domain_id: str | None,
    ) -> tuple[Path, Path]:
        session = self._safe(session_id, "session id")
        snapshot = hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()[:24]
        suffix = "session-start" if domain_id is None else f"domain-{self._safe(domain_id, 'domain id')}"
        directory = self.root / session
        secure_directory(self.root)
        secure_directory(directory)
        final = directory / f"{snapshot}-{suffix}.json"
        return final.with_suffix(".pending.json"), final

    @staticmethod
    def _decode_prepared(path: Path) -> dict[str, object]:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            response = envelope["response"]
            canonical = json.dumps(
                response, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            if envelope.get("receipt_schema_version") != "1.1.0":
                raise ValueError("unsupported prepared delivery receipt")
            if hashlib.sha256(canonical).hexdigest() != envelope.get("response_sha256"):
                raise ValueError("prepared delivery response hash mismatch")
            payload = response.get("payload")
            budget = response.get("budget_bytes")
            if not isinstance(payload, str) or not isinstance(budget, int):
                raise ValueError("prepared delivery response is invalid")
            if len(payload.encode("utf-8")) > budget:
                raise ValueError("prepared delivery exceeds retrieval budget")
            return response
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("prepared delivery receipt is corrupt") from exc

    def prepare_delivery(
        self, session_id: str, snapshot_id: str, domain_id: str | None,
        response: dict[str, object],
    ) -> tuple[str, dict[str, object] | None]:
        """Cache immutable response bytes before the delivery latch is committed.

        Returns ``delivered`` when the latch already exists, otherwise returns
        ``prepared`` and the winning cached response. Concurrent builders always
        converge on the same immutable cache file.
        """
        pending, final = self._paths(session_id, snapshot_id, domain_id)
        if final.exists():
            secure_file(final)
            return "delivered", None
        if pending.exists():
            secure_file(pending)
            return "prepared", self._decode_prepared(pending)
        canonical = json.dumps(
            response, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        envelope = json.dumps({
            "receipt_schema_version": "1.1.0",
            "response": response,
            "response_sha256": hashlib.sha256(canonical).hexdigest(),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=pending.parent)
        temporary = Path(temporary_name)
        os.close(fd)
        try:
            secure_file(temporary)
            with temporary.open("wb") as handle:
                handle.write(envelope)
                handle.flush()
                os.fsync(handle.fileno())
            secure_file(temporary)
            try:
                os.link(temporary, pending)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        if final.exists():
            pending.unlink(missing_ok=True)
            return "delivered", None
        secure_file(pending)
        return "prepared", self._decode_prepared(pending)

    def commit_delivery(
        self, session_id: str, snapshot_id: str, domain_id: str | None,
    ) -> bool:
        """Atomically consume the latch only after a complete response is cached."""
        pending, final = self._paths(session_id, snapshot_id, domain_id)
        if final.exists():
            secure_file(final)
            return False
        secure_file(pending)
        self._decode_prepared(pending)
        try:
            os.link(pending, final)
            secure_file(final)
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                # Delivery is already committed; residue is health-visible but
                # must not turn a successful atomic commit into a false failure.
                pass
            return True
        except FileExistsError:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def reset_session(self, session_id: str) -> None:
        """Forget this session's delivery latches after the host wiped its context."""
        directory = self.root / self._safe(session_id, "session id")
        if not directory.is_dir() or directory.is_symlink():
            return
        for path in directory.glob("*.json"):
            path.unlink(missing_ok=True)

    def delivered_ids(
        self, session_id: str, *, max_files: int = DELIVERED_SCAN_FILES,
        max_bytes: int = DELIVERED_SCAN_BYTES,
    ) -> set[str]:
        """Return record ids committed to this session under any snapshot.

        Only committed 1.1.0 responses count; pending caches were never shown.
        The scan is bounded to the newest ``max_files`` regular files of at
        most ``max_bytes`` each, and a corrupt or foreign file is skipped:
        exclusion is advisory and must never fail a prompt.
        """
        directory = self.root / self._safe(session_id, "session id")
        if not directory.is_dir() or directory.is_symlink():
            return set()
        entries: list[tuple[float, str, Path]] = []
        for path in directory.glob("*.json"):
            if path.name.endswith(".pending.json") or path.name.startswith("."):
                continue
            try:
                status = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(status.st_mode) or status.st_size > max_bytes:
                continue
            entries.append((status.st_mtime, path.name, path))
        entries.sort(key=lambda item: (-item[0], item[1]))
        seen: set[str] = set()
        for _, _, path in entries[:max(0, max_files)]:
            try:
                response = self._decode_prepared(path)
            except Exception:  # corrupt, foreign, or vanished: skip, never fail
                continue
            selected = response.get("selected_ids")
            if isinstance(selected, list):
                seen.update(item for item in selected if isinstance(item, str))
        return seen

    def claim_session_start(self, session_id: str, snapshot_id: str) -> bool:
        return self._claim(session_id, snapshot_id, "session_start", None)

    def claim_domain(self, session_id: str, snapshot_id: str, domain_id: str) -> bool:
        return self._claim(session_id, snapshot_id, "domain", domain_id)
