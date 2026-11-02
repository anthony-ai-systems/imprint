"""Content-free invariant health reporting."""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from datetime import datetime
from pathlib import Path

from imprint.constants import STORE_SCHEMA_VERSION
from imprint.errors import ValidationError
from .report import HealthInputs, HealthReport, evaluate_health


_TEMP_PREFIXES = (
    ".ack-", ".backup-", ".imprint-", ".owner-", ".proposal-",
    ".quarantine-", ".receipt-", ".restore-",
)
_SIDECAR_PREFLIGHT_ERROR = (
    "store has WAL/SHM sidecars; close the active writer or recover the store before use"
)
_DATABASE_RETRY_DELAYS = (0.25, 0.5)
_CAPTURE_SKIP_TAIL_BYTES = 64 * 1024
_CAPTURE_SKIP_WINDOW_SECONDS = 24 * 60 * 60
# A hook failure this old stops degrading health and stays in the counts. Nothing
# here deletes one: pruning is `imprint spool prune`, and it is never automatic.
_HOOK_FAILURE_WINDOW_SECONDS = 7 * 24 * 60 * 60
# Turns lost for a reason a receipt alone would not show. Nested T3 runs and long
# turns reach these in normal use, so counting them is not a fault by itself.
_COUNTED_CAPTURE_SKIPS = (
    "operator_text_oversized",
    "t3_marked_window",
    "t3_prompt_unmatched",
    "t3_scratch_thread",
    "t3_session_ref_unreadable",
    "t3_session_unmatched",
    "transcript_malformed",
    "transcript_refused",
    "transcript_tail_exhausted",
    "transcript_unavailable",
)


def _age_seconds(path: Path, now: float) -> int:
    try:
        return max(0, int(now - path.stat(follow_symlinks=False).st_mtime))
    except OSError:
        return -1


def _latest_age(paths: list[Path], now: float) -> int:
    ages = [_age_seconds(path, now) for path in paths]
    known = [age for age in ages if age >= 0]
    return min(known) if known else -1


def _hook_failure_files(root: Path) -> list[Path]:
    """List persisted hook-failure diagnostics without reading their contents."""
    directory = root / "logs" / "hook-failures"
    if not directory.is_dir() or directory.is_symlink():
        return []
    return [
        path for path in directory.glob("*.json")
        if path.is_file() and not path.is_symlink()
    ]


def _recent_capture_skips(root: Path, reason: str, now: float) -> tuple[int, int]:
    """Count one skip reason over the last day and age the newest, content-free."""
    path = root / "logs" / "capture-skips.jsonl"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(path, flags | getattr(os, "O_NONBLOCK", 0)), "rb") as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode):
                return 0, -1
            offset = max(0, status.st_size - _CAPTURE_SKIP_TAIL_BYTES)
            handle.seek(offset)
            lines = handle.read(_CAPTURE_SKIP_TAIL_BYTES).split(b"\n")
    except OSError:
        return 0, -1
    ages: list[int] = []
    for raw in lines[1:] if offset else lines:  # a tail read may start mid-line
        try:
            entry = json.loads(raw)
            if not isinstance(entry, dict) or entry.get("reason") != reason:
                continue
            stamp = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            ages.append(max(0, int(now - stamp.timestamp())))
        except (KeyError, AttributeError, ValueError, OverflowError, OSError, RecursionError):
            continue
    recent = [age for age in ages if age <= _CAPTURE_SKIP_WINDOW_SECONDS]
    return len(recent), min(ages, default=-1)


def _temporary_residue(root: Path) -> list[Path]:
    if not root.exists():
        return []
    residue: list[Path] = []
    for path in root.rglob("*"):
        name = path.name
        if name.endswith((".tmp", ".pending.json")) or name.startswith(_TEMP_PREFIXES):
            residue.append(path)
    return residue


def _hooks_ok(hook_root: Path, required: set[str]) -> bool:
    if not hook_root.is_dir() or hook_root.is_symlink():
        return False
    for name in required:
        source = hook_root / name
        if source.is_symlink() or not source.is_file():
            return False
        try:
            if source.stat().st_size <= 0:
                return False
        except OSError:
            return False
    manifest = hook_root / "hooks.json"
    if manifest.exists():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if value.get("hook_schema_version") != "1.0.0":
                return False
            declared = {item.get("source") for item in value.get("hooks", []) if isinstance(item, dict)}
            if not required.issubset(declared):
                return False
        except (OSError, AttributeError, json.JSONDecodeError):
            return False
    return True


def _database_health(store) -> tuple[bool, bool, str, set[tuple[str, str]]]:
    """Probe one compatible handle, treating live-writer sidecars as indeterminate."""
    if not store.path.exists():
        return False, False, "failed", set()
    for attempt in range(len(_DATABASE_RETRY_DELAYS) + 1):
        try:
            with store.connect() as conn:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='store_schema_version'"
                ).fetchone()
                consumed = {
                    (str(item[0]), str(item[1]))
                    for item in conn.execute(
                        "SELECT input_event_id, source_path FROM consumed_inputs"
                    ).fetchall()
                }
            database_ok = integrity == "ok"
            migrations_ok = bool(row and row[0] == STORE_SCHEMA_VERSION)
            return (
                database_ok,
                migrations_ok,
                "healthy" if database_ok else "failed",
                consumed,
            )
        except ValidationError as exc:
            if str(exc) != _SIDECAR_PREFLIGHT_ERROR:
                return False, False, "failed", set()
            if attempt < len(_DATABASE_RETRY_DELAYS):
                time.sleep(_DATABASE_RETRY_DELAYS[attempt])
                continue
            return False, False, "busy", set()
        except Exception:
            return False, False, "failed", set()
    raise AssertionError("database retry loop exhausted")


def _spool_is_committed(
    root: Path, path: Path, consumed_inputs: set[tuple[str, str]],
) -> bool:
    """Use exact acknowledgement first, then canonical consumed-input evidence."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            return False
        from imprint.compiler import acknowledgement_committed
        if acknowledgement_committed(root, path, envelope):
            return True
        event_id = envelope.get("input_event_id")
        source_path = path.relative_to(root).as_posix()
        return isinstance(event_id, str) and (event_id, source_path) in consumed_inputs
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return False


def health_report(root: Path, store, config: dict) -> dict[str, object]:
    """Inspect operational invariants without emitting captured content."""
    root = Path(root)
    now = time.time()
    database_ok, migrations_ok, database_state, consumed_inputs = _database_health(store)
    spool_files = [
        path for path in (root / "spool").glob("*/*.json")
        if path.is_file() and not path.is_symlink()
    ] if (root / "spool").exists() else []
    spool_ages = [_age_seconds(path, now) for path in spool_files]
    oldest_spool_age = max((age for age in spool_ages if age >= 0), default=0)
    unacknowledged_spools = [
        path for path in spool_files
        if not _spool_is_committed(root, path, consumed_inputs)
    ]
    unacknowledged_ages = [_age_seconds(path, now) for path in unacknowledged_spools]
    oldest_unacknowledged_spool_age = max(
        (age for age in unacknowledged_ages if age >= 0), default=0,
    )
    configured_retention = config.get("spool_retention_days", 30)
    spool_retention_days = (
        configured_retention
        if isinstance(configured_retention, int) and not isinstance(configured_retention, bool)
        and configured_retention >= 1
        else 30
    )
    configured_hooks = config.get("hooks_dir")
    hook_root = Path(configured_hooks) if isinstance(configured_hooks, str) else Path(__file__).resolve().parents[3] / "hooks"
    required_hooks = {"session_start.py", "user_prompt_submit.py", "stop_capture.py", "health_check.py"}
    hook_parity = _hooks_ok(hook_root, required_hooks)
    disk_free = shutil.disk_usage(root if root.exists() else root.parent).free if (root.exists() or root.parent.exists()) else 0
    from imprint.compiler import compiler_lock_state
    lock_state = compiler_lock_state(root)
    projection_present = (root / "projections" / "imprint.jsonld").is_file()
    acknowledgement_files = [
        path for path in (root / "runtime" / "acknowledgements").glob("*/*.json")
        if path.is_file() and not path.is_symlink()
    ] if (root / "runtime" / "acknowledgements").exists() else []
    delivery_files = [
        path for path in (root / "receipts").glob("*/*.json")
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".pending.json")
    ] if (root / "receipts").exists() else []
    selected_bytes = 0
    omitted_count = 0
    retrieval_budget = int(config.get("context_budget_bytes", 32 * 1024))
    latch_ok = True
    if delivery_files:
        latest_delivery = max(delivery_files, key=lambda path: path.stat().st_mtime)
        try:
            from imprint.retrieve.receipts import DeliveryReceipts
            delivery = DeliveryReceipts._decode_prepared(latest_delivery)
            selected_bytes = int(delivery["selected_bytes"])
            omitted_count = int(delivery["omitted_count"])
            retrieval_budget = int(delivery["budget_bytes"])
        except (OSError, ValueError, KeyError, TypeError):
            latch_ok = False
    backup_verified = False
    verified_backup_count = 0
    invalid_backup_count = 0
    for receipt in (root / "backups").glob("*.receipt.json") if (root / "backups").exists() else ():
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
            backup = receipt.parent / value["file"]
            from imprint.backup import verify_backup
            verified = verify_backup(backup)
            if value.get("backup_schema_version") != "1.0.0" or verified.get("status") != "verified":
                raise ValueError("backup receipt contract invalid")
            verified_backup_count += 1
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            invalid_backup_count += 1
        except Exception:
            # ValidationError is deliberately collapsed into content-free health evidence.
            invalid_backup_count += 1
    backup_verified = verified_backup_count > 0 and invalid_backup_count == 0
    from imprint.permissions import unsafe_private_permissions
    unsafe_permissions = unsafe_private_permissions(root)
    temporary_residue = _temporary_residue(root)
    hook_failure_files = _hook_failure_files(root)
    hook_failure_recent_count = sum(
        # A file whose age cannot be read reports -1, so it counts as recent.
        _age_seconds(path, now) <= _HOOK_FAILURE_WINDOW_SECONDS for path in hook_failure_files
    )
    from imprint.capture.t3_origin import T3_RECORD_UNAVAILABLE_REASON
    t3_unavailable_count, t3_unavailable_age = _recent_capture_skips(
        root, T3_RECORD_UNAVAILABLE_REASON, now,
    )
    capture_skips = {
        reason: _recent_capture_skips(root, reason, now)[0] for reason in _COUNTED_CAPTURE_SKIPS
    }
    from imprint.config import effective_selector_mode
    selector_mode = effective_selector_mode(config.get("selector"))
    selector: dict[str, object] = {
        "mode": selector_mode, "key_present": False, "endpoint_host": "",
        "network_state": "offline" if selector_mode == "deterministic" else "idle",
        "last_transfer_age_seconds": -1, "last_success_ts": "", "fallbacks_24h": 0,
    }
    try:
        # A selector that cannot load reports content-free unknown evidence;
        # health never crashes on it.
        from imprint.retrieve.jev import selector_health
        selector = selector_health(root, config.get("selector"))
    except Exception:
        pass
    t3_homes = None
    if config.get("t3_capture_origin") is True:
        from .t3_homes import t3_home_report
        t3_homes = t3_home_report(root)
    report = evaluate_health(HealthInputs(
        compiler_count=1 if config.get("compiler") else 0,
        database_ok=database_ok,
        migrations_ok=migrations_ok,
        config_ok=True,
        hook_parity_ok=hook_parity,
        spool_depth=len(spool_files),
        oldest_spool_age_seconds=oldest_spool_age,
        unacknowledged_spool_count=len(unacknowledged_spools),
        oldest_unacknowledged_spool_age_seconds=oldest_unacknowledged_spool_age,
        spool_retention_days=spool_retention_days,
        quarantine_count=len(list((root / "quarantine").glob("*.json"))) if (root / "quarantine").exists() else 0,
        hook_failure_count=len(hook_failure_files),
        hook_failure_recent_count=hook_failure_recent_count,
        hook_failure_newest_age_seconds=_latest_age(hook_failure_files, now),
        t3_record_unavailable_24h=t3_unavailable_count,
        capture_skips_24h=capture_skips,
        t3_record_unavailable_newest_age_seconds=t3_unavailable_age,
        t3_homes=t3_homes,
        permissions_ok=not unsafe_permissions,
        unsafe_permission_count=len(unsafe_permissions),
        selected_bytes=selected_bytes,
        retrieval_omitted_count=omitted_count,
        retrieval_budget_bytes=retrieval_budget,
        higher_budget_explicit=bool(config.get("allow_higher_budget", False)),
        experimental_enabled=any(config.get("experimental", {}).values()),
        experimental_state="experimental" if any(config.get("experimental", {}).values()) else "disabled",
        projection_snapshot_present=projection_present,
        latch_ok=latch_ok,
        last_compile_age_seconds=_latest_age(acknowledgement_files, now),
        last_retrieval_age_seconds=_latest_age(delivery_files, now),
        disk_free_bytes=disk_free,
        stale_lock_count=1 if lock_state.get("stale") else 0,
        abandoned_temp_count=len(temporary_residue),
        backup_verified=backup_verified,
        backup_restoreable=backup_verified,
        verified_backup_count=verified_backup_count,
        invalid_backup_count=invalid_backup_count,
        compiler_state=str(lock_state.get("state", "invalid")),
        database_state=database_state,
        network_state=selector["network_state"],
        last_transfer_age_seconds=int(selector["last_transfer_age_seconds"]),
        selector_mode=selector["mode"],
        selector_key_present=bool(selector["key_present"]),
        selector_endpoint_host=str(selector["endpoint_host"]),
        selector_last_success_ts=str(selector["last_success_ts"]),
        selector_fallbacks_24h=int(selector["fallbacks_24h"]),
    ))
    result = report.as_dict()
    # CLI contract uses healthy/degraded while the invariant engine remains green/red.
    result["status"] = "healthy" if report.status == "green" else "degraded"
    return result

__all__ = ["HealthInputs", "HealthReport", "evaluate_health", "health_report"]
