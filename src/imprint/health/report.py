from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Mapping

HEALTH_SCHEMA_VERSION = "1.0.0"
SUPPORTED_RECORD_MAJOR = 3
SUPPORTED_HOOK_MAJOR = 1
# A transcript refused or malformed used to surface as a hook failure. T3 session
# references going unreadable stops all T3 capture. Any one of these needs someone
# to look.
_DEGRADING_CAPTURE_SKIPS = ("t3_session_ref_unreadable", "transcript_malformed", "transcript_refused")


@dataclass(frozen=True)
class HealthInputs:
    compiler_count: int
    database_ok: bool
    migrations_ok: bool
    config_ok: bool = True
    backend_ok: bool = True
    hook_parity_ok: bool = True
    spool_depth: int = 0
    oldest_spool_age_seconds: int = 0
    unacknowledged_spool_count: int | None = None
    oldest_unacknowledged_spool_age_seconds: int | None = None
    spool_stale_after_seconds: int = 3600
    spool_retention_days: int = 30
    quarantine_count: int = 0
    hook_failure_count: int = 0
    hook_failure_recent_count: int | None = None
    hook_failure_newest_age_seconds: int = -1
    t3_record_unavailable_24h: int = 0
    t3_record_unavailable_newest_age_seconds: int = -1
    capture_skips_24h: Mapping[str, int] = field(default_factory=dict)
    permissions_ok: bool = True
    unsafe_permission_count: int = 0
    selected_bytes: int = 0
    omitted_bytes: int = 0
    retrieval_omitted_count: int = 0
    retrieval_budget_bytes: int = 32 * 1024
    higher_budget_explicit: bool = False
    record_schema_major: int = SUPPORTED_RECORD_MAJOR
    hook_schema_major: int = SUPPORTED_HOOK_MAJOR
    experimental_enabled: bool = False
    experimental_state: Literal["disabled", "experimental", "healthy", "stalled"] = "disabled"
    domain_registry_ok: bool = True
    latch_ok: bool = True
    migration_pending: bool = False
    projection_snapshot_present: bool = True
    last_compile_age_seconds: int = 0
    last_retrieval_age_seconds: int = -1
    disk_free_bytes: int = 1
    stale_lock_count: int = 0
    abandoned_temp_count: int = 0
    backup_verified: bool = True
    backup_restoreable: bool = True
    verified_backup_count: int = 0
    invalid_backup_count: int = 0
    compiler_state: Literal["absent", "held", "invalid"] = "absent"
    optional_backend_state: Literal["disabled", "available", "unavailable"] = "disabled"
    network_state: Literal["offline", "idle", "transferred"] = "offline"
    last_transfer_age_seconds: int = -1
    database_state: Literal["healthy", "busy", "failed"] = "healthy"
    selector_mode: Literal["deterministic", "jev", "shadow"] = "deterministic"
    selector_key_present: bool = False
    selector_endpoint_host: str = ""
    selector_last_success_ts: str = ""
    selector_fallbacks_24h: int = 0


@dataclass(frozen=True)
class HealthReport:
    health_schema_version: str
    status: Literal["green", "red"]
    degraded_reasons: tuple[str, ...]
    metrics: dict[str, int | bool | str]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "green" else 1

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_health(values: HealthInputs) -> HealthReport:
    reasons: list[str] = []
    unacknowledged_spool_count = (
        values.spool_depth
        if values.unacknowledged_spool_count is None
        else values.unacknowledged_spool_count
    )
    oldest_unacknowledged_spool_age = (
        values.oldest_spool_age_seconds
        if values.oldest_unacknowledged_spool_age_seconds is None
        else values.oldest_unacknowledged_spool_age_seconds
    )
    hook_failure_recent_count = (
        values.hook_failure_count
        if values.hook_failure_recent_count is None
        else values.hook_failure_recent_count
    )
    if values.compiler_count == 0:
        reasons.append("compiler_missing")
    elif values.compiler_count > 1:
        reasons.append("compiler_duplicate")
    if not values.database_ok and values.database_state != "busy":
        reasons.append("database_integrity_failed")
    if (
        (not values.migrations_ok and values.database_state != "busy")
        or values.migration_pending
    ):
        reasons.append("migration_invalid")
    if not values.config_ok:
        reasons.append("config_invalid")
    if not values.backend_ok:
        reasons.append("required_backend_unavailable")
    if not values.hook_parity_ok:
        reasons.append("hook_parity_failed")
    if (
        unacknowledged_spool_count > 0
        and oldest_unacknowledged_spool_age > values.spool_stale_after_seconds
    ):
        reasons.append("spool_stale")
    if values.quarantine_count > 0:
        reasons.append("quarantine_present")
    if hook_failure_recent_count > 0:
        reasons.append("hook_failures_present")
    if values.t3_record_unavailable_24h > 0:
        reasons.append("t3_record_unavailable")
    reasons.extend(reason for reason in _DEGRADING_CAPTURE_SKIPS if values.capture_skips_24h.get(reason))
    if not values.permissions_ok or values.unsafe_permission_count > 0:
        reasons.append("unsafe_permissions")
    if values.selected_bytes > values.retrieval_budget_bytes:
        reasons.append("retrieval_budget_violated")
    if values.retrieval_budget_bytes > 32 * 1024 and not values.higher_budget_explicit:
        reasons.append("retrieval_budget_unapproved")
    if values.record_schema_major != SUPPORTED_RECORD_MAJOR:
        reasons.append("record_schema_unsupported")
    if values.hook_schema_major != SUPPORTED_HOOK_MAJOR:
        reasons.append("hook_schema_unsupported")
    if not values.domain_registry_ok or not values.latch_ok:
        reasons.append("domain_latch_unsafe")
    if not values.projection_snapshot_present:
        reasons.append("projection_snapshot_missing")
    if values.disk_free_bytes <= 0:
        reasons.append("disk_space_exhausted")
    if values.stale_lock_count > 0:
        reasons.append("stale_lock_present")
    if values.compiler_state == "invalid":
        reasons.append("compiler_lock_invalid")
    if values.abandoned_temp_count > 0:
        reasons.append("abandoned_temp_present")
    if not values.backup_verified or not values.backup_restoreable or values.invalid_backup_count > 0:
        reasons.append("backup_unverified")
    if values.experimental_enabled and values.experimental_state == "stalled":
        reasons.append("experimental_loop_stalled")

    metrics: dict[str, int | bool | str] = {
        "compiler_count": values.compiler_count,
        "database_ok": values.database_ok,
        "database_state": values.database_state,
        "database_evidence": "sqlite_pragma_integrity_check",
        "abandoned_temp_count": max(0, values.abandoned_temp_count),
        "backup_verified": values.backup_verified,
        "backup_restoreable": values.backup_restoreable,
        "verified_backup_count": max(0, values.verified_backup_count),
        "invalid_backup_count": max(0, values.invalid_backup_count),
        "backup_evidence": "receipt_sha256_plus_sqlite_integrity_and_schema",
        "compiler_state": values.compiler_state,
        "compiler_evidence": "configured_authority_plus_compiler_lock",
        "disk_free_bytes": max(0, values.disk_free_bytes),
        "experimental_state": values.experimental_state,
        "higher_budget_explicit": values.higher_budget_explicit,
        "hook_parity_ok": values.hook_parity_ok,
        "hook_evidence": "configured_hook_directory_required_sources",
        "migration_pending": values.migration_pending,
        "migrations_ok": values.migrations_ok,
        "last_compile_age_seconds": max(-1, values.last_compile_age_seconds),
        "last_retrieval_age_seconds": max(-1, values.last_retrieval_age_seconds),
        "last_transfer_age_seconds": values.last_transfer_age_seconds,
        "network_state": values.network_state,
        "oldest_spool_age_seconds": max(0, values.oldest_spool_age_seconds),
        "oldest_unacknowledged_spool_age_seconds": max(0, oldest_unacknowledged_spool_age),
        "omitted_bytes": max(0, values.omitted_bytes),
        "retrieval_omitted_count": max(0, values.retrieval_omitted_count),
        "quarantine_count": max(0, values.quarantine_count),
        "hook_failure_count": max(0, values.hook_failure_count),
        "hook_failure_recent_count": max(0, hook_failure_recent_count),
        "hook_failure_old_count": max(0, values.hook_failure_count - hook_failure_recent_count),
        "hook_failure_newest_age_seconds": max(-1, values.hook_failure_newest_age_seconds),
        "hook_failure_evidence": "operator_logs_hook_failures_file_scan",
        "t3_record_unavailable_24h": max(0, values.t3_record_unavailable_24h),
        "t3_record_unavailable_newest_age_seconds": max(-1, values.t3_record_unavailable_newest_age_seconds),
        "t3_record_evidence": "operator_logs_capture_skips_tail",
        "capture_skips_24h": dict(values.capture_skips_24h),
        "permissions_ok": values.permissions_ok,
        "unsafe_permission_count": max(0, values.unsafe_permission_count),
        "permissions_evidence": "posix_mode_scan_or_platform_acl_contract",
        "optional_backend_state": values.optional_backend_state,
        "projection_snapshot_present": values.projection_snapshot_present,
        "retrieval_budget_bytes": values.retrieval_budget_bytes,
        "selected_bytes": max(0, values.selected_bytes),
        "selector_endpoint_host": values.selector_endpoint_host,
        "selector_evidence": "effective_config_plus_decision_log_tail_and_transfer_marker",
        "selector_fallbacks_24h": max(0, values.selector_fallbacks_24h),
        "selector_key_present": values.selector_key_present,
        "selector_last_success_ts": values.selector_last_success_ts,
        "selector_mode": values.selector_mode,
        "spool_depth": max(0, values.spool_depth),
        "spool_unacknowledged_count": max(0, unacknowledged_spool_count),
        "spool_retention_days": max(1, values.spool_retention_days),
        "spool_evidence": "regular_spool_files_mtime_and_durable_commit_evidence",
        "stale_lock_count": max(0, values.stale_lock_count),
    }
    return HealthReport(
        health_schema_version=HEALTH_SCHEMA_VERSION,
        status="red" if reasons else "green",
        degraded_reasons=tuple(sorted(set(reasons))),
        metrics=metrics,
    )
