"""Command-line surface for the local canonical system."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import signal
import stat
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .compiler import compile_spools, write_envelope
from .config import SELECTOR_STATE_LIMIT, load_config, resolved_operator_root
from .errors import ImprintError, ValidationError
from .projections import markdown_document
from .permissions import secure_directory, secure_file, secure_tree
from .store import ImprintStore


def _store(root: Path) -> ImprintStore:
    return ImprintStore(root / "imprint.db")


def _write_json(value) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))
    sys.stdout.flush()


def _prune_hook_failures(root: Path, *, retention_days: int) -> int:
    """Delete hook-failure diagnostics past retention; never read their contents."""
    directory = root / "logs" / "hook-failures"
    if retention_days < 1 or not directory.is_dir() or directory.is_symlink():
        return 0
    threshold = time.time() - retention_days * 86400
    removed = 0
    for path in sorted(directory.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_mtime >= threshold:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _emit_retrieval_json(value: dict, *, root: Path, session_id: str,
                         snapshot_id: str, domain_id: str | None) -> None:
    """Flush payload at the outermost available boundary before committing it."""
    from .retrieve import commit_payload_delivery

    deferred = os.environ.get("IMPRINT_DEFER_DELIVERY_COMMIT") == "1"
    if deferred:
        value = dict(value)
        value["_imprint_delivery"] = {
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "domain_id": domain_id,
        }
    _write_json(value)
    if not deferred:
        commit_payload_delivery(
            root=root, session_id=session_id, snapshot_id=snapshot_id,
            domain_id=domain_id,
        )


# SessionStart re-fires with these sources after the context window was wiped;
# the memory payload must be refreshed rather than suppressed by the latch.
_REFRESHING_SESSION_SOURCES = frozenset({"resume", "compact"})
_PREVIOUS_REPLY_LABEL = "Previous assistant reply:"
# Wall-clock budget, from the start of main(), for UserPromptSubmit to finish
# its optional selector request. The hook bridge kills the CLI at its
# HOOK_TIMEOUT_SECONDS (10); the rest covers interpreter start-up and the
# deterministic fallback, so a slow endpoint can never empty the hook output.
HOOK_SELECTOR_BUDGET_SECONDS = 6.0


def _selector_state(prompt: str, event: dict, selector: dict) -> str:
    """Turn text for the optional selector; the reply excerpt is best effort.

    When enabled, a prompt shorter than ``below_chars`` is followed by at most
    ``max_chars`` (the tail) of the newest assistant reply, read through the
    bounded transcript reader. Any failure simply omits the excerpt.
    """
    state = prompt[:SELECTOR_STATE_LIMIT]
    try:
        settings = selector["jev"]["short_prompt_context"]
        path = event.get("transcript_path")
        if (
            settings["enabled"] is True
            and len(prompt.strip()) < settings["below_chars"]
            and isinstance(path, str) and path
        ):
            from .capture.transcript import last_assistant_text
            reply = (last_assistant_text(path) or "").strip()
            if reply:
                excerpt = reply[-int(settings["max_chars"]):]
                state = f"{state}\n\n{_PREVIOUS_REPLY_LABEL}\n{excerpt}"
    except Exception:
        state = prompt[:SELECTOR_STATE_LIMIT]
    return state[:SELECTOR_STATE_LIMIT]


def _validate_hook_event(event: dict, expected: str) -> None:
    schema = event.get("hook_schema_version")
    if schema is not None and schema != "1.0.0":
        raise ValidationError("unsupported hook_schema_version")
    native_name = event.get("hook_event_name")
    if native_name is not None and native_name != expected:
        raise ValidationError(f"hook_event_name must be {expected}")
    for field in ("session_id", "sessionId", "cwd", "working_directory", "transcript_path"):
        if field in event and not isinstance(event[field], str):
            raise ValidationError(f"hook field {field} must be a string")


def _operator_urn(root: Path) -> str:
    """Create one local opaque operator identity without deriving personal data."""
    target = root / "identity.json"
    if target.exists():
        secure_file(target)
        value = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(value, dict) and str(value.get("operator_id", "")).startswith("urn:imprint:operator:"):
            return str(value["operator_id"])
        raise ImprintError("operator identity is corrupt")
    secure_directory(root)
    operator_id = f"urn:imprint:operator:{uuid.uuid4()}"
    fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=root)
    temporary_path = Path(temporary)
    os.close(fd)
    try:
        secure_file(temporary_path)
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump({"identity_schema_version": "1.0.0", "operator_id": operator_id}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(temporary_path)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        try:
            os.link(temporary, target)
        except FileExistsError:
            return _operator_urn(root)
    finally:
        temporary_path.unlink(missing_ok=True)
    secure_file(target)
    return operator_id


def _session_key(root: Path) -> bytes:
    """Return an installation-local key without persisting provider session IDs."""
    target = root / "session-map.key"
    if target.exists():
        secure_file(target)
        try:
            encoded = target.read_text(encoding="ascii").strip()
            key = bytes.fromhex(encoded)
        except (OSError, ValueError) as exc:
            raise ImprintError("session mapping key is corrupt") from exc
        if len(key) != 32:
            raise ImprintError("session mapping key is corrupt")
        return key
    secure_directory(root)
    encoded = secrets.token_hex(32)
    fd, temporary = tempfile.mkstemp(prefix=".session-map-", dir=root)
    temporary_path = Path(temporary)
    os.close(fd)
    try:
        secure_file(temporary_path)
        with temporary_path.open("w", encoding="ascii") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(temporary_path)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        try:
            os.link(temporary, target)
        except FileExistsError:
            return _session_key(root)
    finally:
        temporary_path.unlink(missing_ok=True)
    secure_file(target)
    return bytes.fromhex(encoded)


def _opaque_session_urn(root: Path, native_session_id: str) -> str:
    """Map a native session to a stable opaque UUID without storing the input."""
    digest = hmac.new(
        _session_key(root), native_session_id.encode("utf-8"), hashlib.sha256,
    ).digest()
    mapped = uuid.UUID(bytes=digest[:16], version=4)
    return f"urn:imprint:session:{mapped}"


def _record_capture_skip(root: Path, reason: str) -> None:
    """Leave a content-free trace of a skip that would otherwise vanish. Never raises."""
    line = json.dumps({
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }, sort_keys=True) + "\n"
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        secure_directory(root / "logs")
        descriptor = os.open(root / "logs" / "capture-skips.jsonl", flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                handle.write(line)
    except OSError:
        pass


def _tail_turns(snapshot, host_origin) -> tuple:
    """Read the complete lines of a tail, which may start part way through one."""
    from .capture.transcript import collect_turns
    from .errors import ValidationError

    tail = snapshot.data
    if snapshot.offset:
        newline = tail.find(b"\n")
        tail = tail[newline + 1:] if newline >= 0 else b""
    try:
        decoded_lines = [line.decode("utf-8") for line in tail.splitlines(keepends=True)]
    except UnicodeDecodeError as exc:
        raise ValidationError("transcript tail is not valid UTF-8") from exc
    return (tail, *collect_turns(decoded_lines, host_origin))


def _found_no_prompt(snapshot, turns: tuple) -> bool:
    """Whether a read that did not reach the start of the file holds no prompt."""
    _, messages, _, unverified_since_operator = turns
    asked = unverified_since_operator or any(message[0] == "user" for message in messages)
    return bool(snapshot.offset) and not asked


def _parse_large_native_transcript(path_value: str, *, snapshot=None, host_origin=None) -> dict:
    """Recover final feedback from a huge transcript using a bounded tail read.

    A long turn can push its prompt out of the first read, so a read with no prompt
    in it is repeated once at the full bound, and one that still finds none says so.
    """
    from .capture.provenance import UNVERIFIED_BASIS, skip_reason
    from .capture.transcript import (
        MAX_CONTEXT_BYTES,
        MAX_TRANSCRIPT_BYTES,
        TAIL_READ_BYTES,
        TRANSCRIPT_TAIL_EXHAUSTED_REASON,
        _read_native_transcript_snapshot,
        _truncate_utf8,
    )

    if snapshot is None:
        snapshot = _read_native_transcript_snapshot(path_value, tail_limit=TAIL_READ_BYTES)
    turns = _tail_turns(snapshot, host_origin)
    if _found_no_prompt(snapshot, turns):
        snapshot = _read_native_transcript_snapshot(path_value, tail_limit=MAX_TRANSCRIPT_BYTES)
        turns = _tail_turns(snapshot, host_origin)
    tail, messages, synthetic_basis, unverified_since_operator = turns
    if _found_no_prompt(snapshot, turns):
        return {
            "operator_text": None,
            "prior_assistant_output": None,
            "case_description": None,
            "source_locator": f"transcript-tail:sha256:{hashlib.sha256(tail).hexdigest()}",
            "skip_reason": TRANSCRIPT_TAIL_EXHAUSTED_REASON,
            "provenance_basis": None,
            "source_entry_id": None,
        }
    size, offset = snapshot.size, snapshot.offset
    user_indexes = [index for index, message in enumerate(messages) if message[0] == "user"]
    if not user_indexes or unverified_since_operator:
        # A tail with no operator turn is skipped, not raised: a raised Stop
        # hook exits 2 and blocks the host session. A newer prompt nobody can
        # vouch for ends the search too: an older turn is not what this Stop
        # answered.
        basis = UNVERIFIED_BASIS if unverified_since_operator else synthetic_basis
        return {
            "operator_text": None,
            "prior_assistant_output": None,
            "case_description": None,
            "source_locator": f"transcript-tail:sha256:{hashlib.sha256(tail).hexdigest()}",
            "skip_reason": skip_reason(basis),
            "provenance_basis": basis,
            "source_entry_id": None,
        }
    user_index = user_indexes[-1]
    operator_text = messages[user_index][1]
    prior_assistant = next(
        (messages[index][1] for index in range(user_index - 1, -1, -1)
         if messages[index][0] == "assistant"),
        None,
    )
    context_truncated = False
    if prior_assistant is not None:
        prior_assistant, context_truncated = _truncate_utf8(prior_assistant, MAX_CONTEXT_BYTES)
    evidence_sha256 = hashlib.sha256(tail).hexdigest()
    return {
        "operator_text": operator_text,
        "prior_assistant_output": prior_assistant,
        "case_description": "Explicit operator feedback witnessed in a bounded Claude Code transcript tail",
        "source_locator": f"transcript-tail:sha256:{evidence_sha256}",
        "skip_reason": None,
        "provenance_basis": messages[user_index][3],
        "source_entry_id": messages[user_index][2],
        "source_timestamp": messages[user_index][4],
        "degradation": {
            "schema_version": "1.0.0",
            "payload": {
                "transcript_bytes": size,
                "tail_bytes_examined": len(tail),
                "evidence_sha256": evidence_sha256,
                "hash_scope": "bounded_tail",
                "truncated": True,
                "context_truncated": context_truncated or offset > 0,
                "receipt": "huge_transcript_bounded_tail",
            },
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imprint", description="Local-first captured judgment")
    parser.add_argument("--config", type=Path)
    subs = parser.add_subparsers(dest="command", required=True)

    capture = subs.add_parser("capture", help="write a validated event to the immutable spool")
    capture.add_argument("--event", required=True, type=Path)

    compile_cmd = subs.add_parser("compile", help="compile immutable spools into canonical state")
    compile_cmd.add_argument("--once", action="store_true")
    compile_cmd.add_argument("--recover-stale-lock", metavar="NONCE")

    spool = subs.add_parser("spool", help="manage only this producer's acknowledged inputs")
    spool_subs = spool.add_subparsers(dest="spool_action", required=True)
    spool_prune = spool_subs.add_parser("prune")
    spool_prune.add_argument("--retention-days", type=int)

    derive = subs.add_parser("derive", help="submit or compile non-authoritative proposals")
    derive_group = derive.add_mutually_exclusive_group(required=True)
    derive_group.add_argument("--submit", metavar="PROPOSAL", type=Path)
    derive_group.add_argument("--pending", action="store_true")

    export = subs.add_parser("export", help="export current canonical state")
    export.add_argument("--format", choices=("jsonld", "markdown"), required=True)
    export.add_argument("--output", type=Path)

    import_cmd = subs.add_parser("import", help="import into an empty compatible canonical store")
    import_cmd.add_argument("--format", choices=("jsonld",), required=True)
    import_cmd.add_argument("--input", type=Path, required=True)
    import_cmd.add_argument("--dry-run", action="store_true")

    ingest = subs.add_parser("ingest", help="quarantine and rule on imported material")
    ingest_subs = ingest.add_subparsers(dest="ingest_action", required=True)
    ingest_scan = ingest_subs.add_parser("scan")
    ingest_scan.add_argument("--input", type=Path, required=True)
    ingest_show = ingest_subs.add_parser("show")
    ingest_show.add_argument("item_id")
    ingest_keep = ingest_subs.add_parser("keep")
    ingest_keep.add_argument("item_id")
    ingest_keep.add_argument("--why", required=True)
    ingest_kill = ingest_subs.add_parser("kill")
    ingest_kill.add_argument("item_id")
    ingest_kill.add_argument("--why")
    ingest_subs.add_parser("status")

    migrate = subs.add_parser("migrate", help="plan, apply, or verify additive migrations")
    migrate_subs = migrate.add_subparsers(dest="migrate_action", required=True)
    for action in ("plan", "apply"):
        command = migrate_subs.add_parser(action)
        command.add_argument("--spec", type=Path, required=True)
    migrate_subs.add_parser("verify")
    migrate_subs.add_parser(
        "ontology-report",
        help="verify ontology compatibility and classify opaque legacy records",
    )

    review = subs.add_parser("review", help="inspect and explicitly dispose of derived proposals")
    review_subs = review.add_subparsers(dest="review_action", required=True)
    review_subs.add_parser("list")
    review_show = review_subs.add_parser("show")
    review_show.add_argument("node_id")
    review_ratify = review_subs.add_parser("ratify")
    review_ratify.add_argument("node_id")
    review_ratify.add_argument("--by", required=True)
    review_ratify.add_argument("--note", default="")
    review_reject = review_subs.add_parser("reject")
    review_reject.add_argument("node_id")
    review_reject.add_argument("--by", required=True)
    review_reject.add_argument("--reason", required=True)
    review_defer = review_subs.add_parser("defer")
    review_defer.add_argument("node_id")
    review_defer.add_argument("--by", required=True)
    review_defer.add_argument("--reason", required=True)
    review_defer.add_argument("--revisit-after")
    review_correct = review_subs.add_parser("correct")
    review_correct.add_argument("node_id")
    review_correct.add_argument("--by", required=True)
    review_correct.add_argument("--reason", required=True)
    review_correct.add_argument("--input", type=Path, required=True)
    review_contest = review_subs.add_parser("contest")
    review_contest.add_argument("node_id")
    review_contest.add_argument("--by", required=True)
    review_contest.add_argument("--reason", required=True)
    for action in ("edge-ratify", "edge-reject", "edge-defer"):
        command = review_subs.add_parser(action)
        command.add_argument("edge_id")
        command.add_argument("--by", required=True)
        if action == "edge-ratify":
            command.add_argument("--note", default="")
        else:
            command.add_argument("--reason", required=True)
        if action == "edge-defer":
            command.add_argument("--revisit-after")

    ratify = subs.add_parser("ratify", help="apply a card's recorded answers to a ratification packet")
    ratify_subs = ratify.add_subparsers(dest="ratify_action", required=True)
    ratify_apply = ratify_subs.add_parser("apply")
    ratify_apply.add_argument("--packet", type=Path, required=True)
    ratify_apply.add_argument("--out-root", type=Path, required=True,
                              help="folder for receipts and scope-bindings manifests")
    ratify_apply.add_argument("--reader", type=Path, required=True,
                              help="native judgment reader run read-only for the readback")
    ratify_apply.add_argument("--card-store", type=Path,
                              help="card store asks.json, read only; default $AGENTS_CARD_STORE")
    ratify_apply.add_argument("--dry-run", action="store_true")

    ontology = subs.add_parser("ontology", help="write closed, versioned semantic contracts")
    ontology_subs = ontology.add_subparsers(dest="ontology_action", required=True)
    ontology_node = ontology_subs.add_parser("add-node")
    ontology_node.add_argument("--input", type=Path, required=True)
    ontology_node.add_argument("--valid-from", required=True)
    ontology_relation = ontology_subs.add_parser("add-relation")
    ontology_relation.add_argument("--input", type=Path, required=True)
    ontology_relation.add_argument("--valid-from", required=True)

    for command_name, help_text in (
        ("observation", "write a closed observed-evidence contract"),
        ("outcome", "write a closed measured-outcome contract"),
    ):
        command = subs.add_parser(command_name, help=help_text)
        command_subs = command.add_subparsers(dest=f"{command_name}_action", required=True)
        add = command_subs.add_parser("add")
        add.add_argument("--input", type=Path, required=True)
        add.add_argument("--valid-from", required=True)

    consent = subs.add_parser("consent", help="grant, inspect, or revoke durable capture consent")
    consent_subs = consent.add_subparsers(dest="consent_action", required=True)
    consent_grant = consent_subs.add_parser("grant")
    consent_grant.add_argument("--input", type=Path, required=True)
    consent_grant.add_argument("--valid-from", required=True)
    consent_subs.add_parser("list")
    consent_revoke = consent_subs.add_parser("revoke")
    consent_revoke.add_argument("grant_id")
    consent_revoke.add_argument("--by", required=True)
    consent_revoke.add_argument("--reason", required=True)
    consent_revoke.add_argument("--revoked-at")

    history = subs.add_parser("history", help="show immutable versions and dispositions")
    history.add_argument("node_id")

    verdict = subs.add_parser("verdict", help="append later WHY or reinforcement evidence")
    verdict_subs = verdict.add_subparsers(dest="verdict_action", required=True)
    add_reason = verdict_subs.add_parser("add-reason")
    add_reason.add_argument("node_id")
    add_reason.add_argument("--reason", required=True)
    add_reason.add_argument("--by", required=True)
    add_reason.add_argument("--source", default="explicit_cli")
    reinforce = verdict_subs.add_parser("reinforce")
    reinforce.add_argument("node_id")
    reinforce.add_argument("--evidence", required=True)
    reinforce.add_argument("--by", required=True)
    reinforce.add_argument("--source", default="explicit_cli")

    domain = subs.add_parser("domain", help="manage canonical Domain ontology nodes")
    domain_subs = domain.add_subparsers(dest="domain_action", required=True)
    domain_add = domain_subs.add_parser("add")
    domain_add.add_argument("domain_id")
    domain_add.add_argument("--label", required=True)
    domain_add.add_argument("--description", required=True)
    domain_add.add_argument("--evidence", action="append", required=True)
    domain_add.add_argument("--by", required=True)
    domain_add.add_argument("--valid-from")
    domain_subs.add_parser("list")
    domain_select = domain_subs.add_parser("select")
    domain_select.add_argument("domain_id")
    domain_select.add_argument("--by", required=True)
    domain_freeze = domain_subs.add_parser("freeze")
    domain_freeze.add_argument("domain_id")
    domain_freeze.add_argument("--by", required=True)

    transition = subs.add_parser("transition", help="record canonical contradiction or supersession edges")
    transition_subs = transition.add_subparsers(dest="transition_action", required=True)
    for action in ("contradict", "supersede"):
        command = transition_subs.add_parser(action)
        command.add_argument("source_id", help="asserting node; for supersede, the replacement")
        command.add_argument("target_id", help="affected node; for supersede, the prior node retired")
        command.add_argument("--reason", required=True)
        command.add_argument("--evidence", action="append", required=True)
        command.add_argument("--by", required=True)

    backup = subs.add_parser("backup", help="create, verify, or safely restore a backup")
    backup_subs = backup.add_subparsers(dest="backup_action", required=True)
    backup_create = backup_subs.add_parser("create")
    backup_create.add_argument("--output", type=Path)
    backup_verify = backup_subs.add_parser("verify")
    backup_verify.add_argument("path", type=Path)
    backup_restore = backup_subs.add_parser("restore")
    backup_restore.add_argument("path", type=Path)
    backup_restore.add_argument("--confirm", required=True)

    delete = subs.add_parser("delete", help="tombstone or explicitly hard-purge data")
    delete_subs = delete.add_subparsers(dest="delete_action", required=True)
    tombstone = delete_subs.add_parser("tombstone")
    tombstone.add_argument("--scope", required=True)
    tombstone.add_argument("--reason", required=True)
    purge = delete_subs.add_parser("purge")
    purge.add_argument("--scope", required=True)
    purge.add_argument("--confirm")
    purge.add_argument("--sentinel")
    purge.add_argument("--preview", action="store_true")

    profile = subs.add_parser("profile", help="store a non-load-bearing onboarding profile seed")
    profile_subs = profile.add_subparsers(dest="profile_action", required=True)
    profile_seed = profile_subs.add_parser("seed")
    profile_seed.add_argument("--input", type=Path, required=True)
    profile_seed.add_argument("--evidence", action="append", default=[])
    profile_seed.add_argument("--valid-from", required=True)

    experimental = subs.add_parser("experimental", help="report disabled/unverified feature status")
    experimental.add_subparsers(dest="experimental_action", required=True).add_parser("status")

    retrieve = subs.add_parser("retrieve", help="build a bounded context payload")
    retrieve.add_argument("--session", required=True)
    retrieve.add_argument("--prompt", default="")
    retrieve.add_argument("--domain")
    retrieve.add_argument("--refresh", action="store_true")

    store_cmd = subs.add_parser("store", help="inspect or recover the local canonical store")
    store_subs = store_cmd.add_subparsers(dest="store_action", required=True)
    store_subs.add_parser(
        "recover",
        help="replay orphaned WAL crash residue from a hard kill without losing committed captures",
    )

    hook = subs.add_parser("hook", help="execute one portable hook action")
    hook.add_argument("action", choices=("session-start", "user-prompt-submit", "stop-capture", "health-check"))

    subs.add_parser("delivery-commit", help=argparse.SUPPRESS)

    subs.add_parser("health", help="report content-free health as JSON")
    subs.add_parser("version", help="print version")
    return parser


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    parser = build_parser()
    args = parser.parse_args(argv)
    # Facts the failure receipt must carry out of a partially completed action.
    error_details: dict[str, object] = {}
    try:
        # Machine transport is authoritative even if a payload claims human
        # origin. Skip before resolving operator identity or touching the store.
        transport_origin = os.environ.get("IMPRINT_CAPTURE_ORIGIN", "").strip().lower()
        if args.command == "hook" and args.action == "stop-capture" and transport_origin:
            from .capture.provenance import MACHINE_ORIGIN_REASON, UNVERIFIED_ENTRY_REASON
            event = json.load(sys.stdin)
            if not isinstance(event, dict):
                raise ImprintError("hook event must be an object")
            _validate_hook_event(event, "Stop")
            _write_json({"hook_schema_version": "1.0.0", "status": "skipped",
                         "reason": MACHINE_ORIGIN_REASON if transport_origin == "automation"
                         else UNVERIFIED_ENTRY_REASON, "provenance_basis": "transport"})
            return 0
        config = load_config(args.config)
        root = resolved_operator_root(config)
        store = _store(root)
        if args.command == "version":
            from . import __version__
            print(__version__)
            return 0
        if args.command == "ratify":
            # Ratification writes authority: name the store explicitly, and never mint an identity.
            if args.config is None or not args.config.is_file():
                raise ValidationError("ratify requires --config naming an existing Imprint config")
            from .ratify import Refusal, apply_packet
            try:
                result = apply_packet(
                    root=root, packet_path=args.packet, out_root=args.out_root,
                    reader_path=args.reader, card_store=args.card_store, dry_run=args.dry_run,
                )
            except Refusal as exc:
                _write_json({"status": "refused", "reasons": exc.reasons})
                return 2
            _write_json(result)
            return 0 if result["status"] in {"applied", "already_applied", "dry_run"} and not result.get("error") else 2
        store.expected_operator_id = _operator_urn(root)
        store.expected_node_id = str(config.get("node_id", "primary"))
        if args.command == "delivery-commit":
            from .retrieve import commit_payload_delivery
            receipt = json.load(sys.stdin)
            if not isinstance(receipt, dict) or set(receipt) != {"session_id", "snapshot_id", "domain_id"}:
                raise ValidationError("delivery commit receipt is invalid")
            if not isinstance(receipt["session_id"], str) or not isinstance(receipt["snapshot_id"], str):
                raise ValidationError("delivery commit identity is invalid")
            if receipt["domain_id"] is not None and not isinstance(receipt["domain_id"], str):
                raise ValidationError("delivery commit domain is invalid")
            committed = commit_payload_delivery(root=root, **receipt)
            _write_json({"status": "committed" if committed else "already_committed"})
            return 0
        if args.command == "capture":
            envelope = json.loads(args.event.read_text())
            if envelope.get("operator_id") != store.expected_operator_id or envelope.get("node_id") != store.expected_node_id:
                raise ValidationError("capture operator/node does not match configured identity")
            path = write_envelope(root, envelope)
            _write_json({"status": "queued", "path": str(path)})
            return 0
        if args.command == "compile":
            if args.recover_stale_lock:
                from .compiler import recover_stale_compiler_lock
                _write_json(recover_stale_compiler_lock(root, confirmation=args.recover_stale_lock))
                return 0
            counts = compile_spools(root, store, compiler_authorized=bool(config.get("compiler")))
            _write_json({"status": "ok", **counts})
            return 0 if counts["quarantined"] == 0 else 2
        if args.command == "spool":
            from .compiler import prune_acknowledged_spools
            days = args.retention_days
            if days is None:
                days = int(config.get("spool_retention_days", 30))
            result = prune_acknowledged_spools(
                root, source_node_id=str(config.get("node_id", "primary")),
                retention_days=days,
            )
            hook_failures_pruned = _prune_hook_failures(root, retention_days=days)
            _write_json({
                "status": "ok" if result["invalid"] == 0 else "degraded",
                **result,
                "hook_failures_pruned": hook_failures_pruned,
            })
            return 0 if result["invalid"] == 0 else 2
        if args.command == "derive":
            from .derive import ProposalSpoolWriter, compile_pending_proposals
            if args.submit:
                proposal = json.loads(args.submit.read_text(encoding="utf-8"))
                if not isinstance(proposal, dict):
                    raise ValidationError("proposal input must be an object")
                proposal_id = ProposalSpoolWriter(root).submit_proposal(proposal)
                _write_json({"status": "queued", "proposal_id": proposal_id})
                return 0
            result = compile_pending_proposals(root, store)
            _write_json({"status": "ok" if result["rejected"] == 0 else "degraded", **result})
            return 0 if result["rejected"] == 0 else 2
        if args.command == "export":
            from .portability import export_jsonld
            store.initialize()
            snapshot = store.snapshot()
            if args.format == "jsonld":
                content = json.dumps(export_jsonld(store), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            else:
                content = markdown_document(snapshot)
            if args.output:
                if args.output.parent.exists():
                    if args.output.parent.is_symlink() or not args.output.parent.is_dir():
                        raise ValidationError("export parent must be a regular directory")
                else:
                    secure_directory(args.output.parent)
                args.output.write_text(content)
                secure_file(args.output)
                if root.exists():
                    secure_tree(root)
                _write_json({"status": "exported", "path": str(args.output)})
            else:
                print(content, end="")
            return 0
        if args.command == "import":
            from .portability import import_jsonld
            document = json.loads(args.input.read_text(encoding="utf-8"))
            digest = import_jsonld(store, document, dry_run=args.dry_run)
            _write_json({"status": "validated" if args.dry_run else "imported", "semantic_sha256": digest})
            return 0
        if args.command == "ingest":
            from .ingest import IngestCandidate, IngestService
            service = IngestService(store, _operator_urn(root))
            if args.ingest_action == "scan":
                raw = json.loads(args.input.read_text(encoding="utf-8"))
                values = raw if isinstance(raw, list) else [raw]
                expected = {"source_kind", "source_locator", "content", "metadata", "extensions"}
                candidates = []
                for value in values:
                    if not isinstance(value, dict) or not set(value).issubset(expected):
                        raise ValidationError("ingest input contains unknown fields or is not an object")
                    required = expected - {"extensions"}
                    if not required.issubset(value):
                        raise ValidationError("ingest input is missing required fields")
                    candidates.append(IngestCandidate(**value))
                _write_json({"status": "quarantined", "items": service.scan(candidates)})
                return 0
            if args.ingest_action == "show":
                items = [item for item in service.list() if item["item_id"] == args.item_id]
                if not items:
                    raise ValidationError("unknown ingest item")
                _write_json({"status": "ok", "item": items[0]})
                return 0
            if args.ingest_action == "keep":
                ruling_id = service.keep(args.item_id, why=args.why)
                _write_json({"status": "kept", "ruling_id": ruling_id})
                return 0
            if args.ingest_action == "kill":
                ruling_id = service.kill(args.item_id, why=args.why)
                _write_json({"status": "killed", "ruling_id": ruling_id})
                return 0
            items = service.list()
            counts = {state: sum(item["status"] == state for item in items) for state in ("unruled", "kept", "killed")}
            _write_json({"status": "ok", "counts": counts})
            return 0
        if args.command == "migrate":
            from .portability import (
                Migration,
                MigrationRunner,
                ontology_migration_report,
                verify_ontology_schema,
            )
            if args.migrate_action == "ontology-report":
                result = ontology_migration_report(store)
                _write_json(result)
                return 0 if result["status"] in {"current", "migration_available"} else 2
            if args.migrate_action == "verify":
                if not store.path.exists():
                    store.initialize()
                with store.connect() as conn:
                    version = conn.execute("SELECT value FROM meta WHERE key='store_schema_version'").fetchone()[0]
                    receipts = conn.execute("SELECT migration_id,result_sha256 FROM migrations ORDER BY applied_at,migration_id").fetchall()
                ontology = verify_ontology_schema(store)
                healthy = store.integrity_check() == "ok" and ontology["compatible"]
                result = {"status": "ok" if healthy else "error", "store_schema_version": version, "ontology": ontology, "migrations": [dict(row) for row in receipts]}
                _write_json(result)
                return 0 if result["status"] == "ok" else 2
            runner = MigrationRunner(store)
            spec = json.loads(args.spec.read_text(encoding="utf-8"))
            expected = {"migration_id", "from_version", "to_version", "statements", "backup_receipt"}
            if not isinstance(spec, dict) or set(spec) != expected or not isinstance(spec["statements"], list):
                raise ValidationError("migration spec has invalid fields")
            migration = Migration(
                migration_id=spec["migration_id"], from_version=spec["from_version"],
                to_version=spec["to_version"], statements=tuple(spec["statements"]),
                backup_receipt=spec["backup_receipt"],
            )
            if args.migrate_action == "plan":
                with store.connect() as conn:
                    current = conn.execute("SELECT value FROM meta WHERE key='store_schema_version'").fetchone()[0]
                _write_json({"status": "applicable" if current == migration.from_version else "blocked", "current": current, "from": migration.from_version, "to": migration.to_version, "code_sha256": migration.code_sha256})
                return 0 if current == migration.from_version else 2
            _write_json({"status": runner.apply(migration), "migration_id": migration.migration_id})
            return 0
        if args.command == "review":
            from .lifecycle import review_list, review_show
            store.initialize()
            if args.review_action == "list":
                items = review_list(store)
                _write_json({"status": "ok", "count": len(items), "items": items})
                return 0
            if args.review_action == "show":
                _write_json({"status": "ok", "item": review_show(store, args.node_id)})
                return 0
            if args.review_action == "ratify":
                event_id = store.ratify_node(args.node_id, ratifier=args.by, note=args.note)
                _write_json({"status": "ratified", "event_id": event_id, "node_id": args.node_id})
                return 0
            if args.review_action == "defer":
                event_id = store.defer_node(
                    args.node_id, reviewer=args.by, reason=args.reason,
                    revisit_after=args.revisit_after,
                )
                _write_json({"status": "deferred", "event_id": event_id, "node_id": args.node_id})
                return 0
            if args.review_action == "correct":
                contract = json.loads(args.input.read_text(encoding="utf-8"))
                if not isinstance(contract, dict):
                    raise ValidationError("corrected ontology input must be one JSON object")
                event_id = store.correct_typed_node(
                    args.node_id, corrected_contract=contract,
                    corrector=args.by, reason=args.reason,
                )
                _write_json({"status": "corrected", "event_id": event_id, "node_id": args.node_id})
                return 0
            if args.review_action == "contest":
                event_id = store.contest_typed_node(
                    args.node_id, contestor=args.by, reason=args.reason,
                )
                _write_json({"status": "contested", "event_id": event_id, "node_id": args.node_id})
                return 0
            if args.review_action == "edge-ratify":
                event_id = store.ratify_edge(args.edge_id, ratifier=args.by, note=args.note)
                _write_json({"status": "edge_ratified", "event_id": event_id, "edge_id": args.edge_id})
                return 0
            if args.review_action == "edge-defer":
                event_id = store.defer_edge(
                    args.edge_id, reviewer=args.by, reason=args.reason,
                    revisit_after=args.revisit_after,
                )
                _write_json({"status": "edge_deferred", "event_id": event_id, "edge_id": args.edge_id})
                return 0
            if args.review_action == "edge-reject":
                event_id = store.reject_edge(args.edge_id, rejector=args.by, reason=args.reason)
                _write_json({"status": "edge_rejected", "event_id": event_id, "edge_id": args.edge_id})
                return 0
            event_id = store.reject_node(args.node_id, rejector=args.by, reason=args.reason)
            _write_json({"status": "rejected", "event_id": event_id, "node_id": args.node_id})
            return 0
        if args.command == "ontology":
            store.initialize()
            value = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValidationError("ontology input must be one JSON object")
            if value.get("operator_id") != store.expected_operator_id:
                raise ValidationError("ontology contract operator does not match configured identity")
            if args.ontology_action == "add-node":
                identifier = store.append_semantic_node(value, valid_from=args.valid_from)
                _write_json({"status": "semantic_node_added", "node_id": identifier})
            else:
                identifier = store.append_semantic_relation(value, valid_from=args.valid_from)
                _write_json({"status": "semantic_relation_added", "relation_id": identifier})
            return 0
        if args.command in {"observation", "outcome"}:
            store.initialize()
            value = json.loads(args.input.read_text(encoding="utf-8"))
            expected_type = "Observation" if args.command == "observation" else "Outcome"
            if not isinstance(value, dict) or value.get("node_type") != expected_type:
                raise ValidationError(f"{args.command} add requires one {expected_type} contract")
            if value.get("operator_id") != store.expected_operator_id:
                raise ValidationError(f"{args.command} contract operator does not match configured identity")
            identifier = store.append_semantic_node(value, valid_from=args.valid_from)
            _write_json({"status": f"{args.command}_added", "node_id": identifier})
            return 0
        if args.command == "consent":
            store.initialize()
            if args.consent_action == "grant":
                value = json.loads(args.input.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("node_type") != "ConsentGrant":
                    raise ValidationError("consent grant requires one ConsentGrant contract")
                if value.get("operator_id") != store.expected_operator_id:
                    raise ValidationError("consent contract operator does not match configured identity")
                identifier = store.append_semantic_node(value, valid_from=args.valid_from)
                _write_json({"status": "consent_granted", "grant_id": identifier})
                return 0
            if args.consent_action == "list":
                items = store.current_nodes(["ConsentGrant"])
                _write_json({"status": "ok", "count": len(items), "items": items})
                return 0
            event_id = store.revoke_consent(
                args.grant_id, operator_id=args.by, reason=args.reason,
                revoked_at=args.revoked_at,
            )
            _write_json({"status": "consent_revoked", "event_id": event_id, "grant_id": args.grant_id})
            return 0
        if args.command == "history":
            store.initialize()
            _write_json({"status": "ok", "history": store.node_history(args.node_id)})
            return 0
        if args.command == "verdict":
            store.initialize()
            if args.verdict_action == "add-reason":
                event_id = store.add_reason(
                    args.node_id, reason=args.reason, actor_id=args.by, source_locator=args.source,
                )
                status = "reason_added"
            else:
                event_id = store.reinforce_verdict(
                    args.node_id, evidence_text=args.evidence, actor_id=args.by, source_locator=args.source,
                )
                status = "reinforced"
            _write_json({"status": status, "event_id": event_id, "node_id": args.node_id})
            return 0
        if args.command == "domain":
            store.initialize()
            if args.domain_action == "add":
                node_id = store.add_domain(
                    domain_id=args.domain_id, public_label=args.label,
                    description=args.description, evidence_ids=args.evidence,
                    operator_id=_operator_urn(root), actor_id=args.by,
                    valid_from=args.valid_from,
                )
                _write_json({"status": "domain_added", "node_id": node_id, "domain_id": args.domain_id})
                return 0
            if args.domain_action == "list":
                items = store.list_domains()
                _write_json({"status": "ok", "count": len(items), "items": items})
                return 0
            if args.domain_action == "select":
                event_id = store.select_domain(args.domain_id, actor_id=args.by)
                _write_json({"status": "domain_selected", "event_id": event_id, "domain_id": args.domain_id})
                return 0
            event_id = store.freeze_domain(args.domain_id, actor_id=args.by)
            _write_json({"status": "domain_frozen", "event_id": event_id, "domain_id": args.domain_id})
            return 0
        if args.command == "transition":
            store.initialize()
            relation = {"contradict": "contradicts", "supersede": "supersedes"}[args.transition_action]
            edge_id = store.add_transition(
                relation, args.source_id, args.target_id, reason=args.reason,
                evidence_ids=args.evidence, actor_id=args.by,
            )
            _write_json({"status": relation, "edge_id": edge_id, "source_id": args.source_id, "target_id": args.target_id})
            return 0
        if args.command == "backup":
            from .backup import create_backup, restore_backup, verify_backup
            if args.backup_action == "create":
                store.initialize()
                _write_json({"status": "created", **create_backup(store, root, args.output)})
                return 0
            if args.backup_action == "verify":
                _write_json(verify_backup(args.path))
                return 0
            _write_json(restore_backup(store, root, args.path, confirmation=args.confirm))
            return 0
        if args.command == "delete":
            store.initialize()
            if args.delete_action == "tombstone":
                event_id = store.tombstone_node(args.scope, reason=args.reason)
                _write_json({"status": "tombstoned", "event_id": event_id, "node_id": args.scope})
                return 0
            from .purge import hard_purge, preview_purge
            if args.preview:
                _write_json({"status": "preview", **preview_purge(store, root, args.scope)})
                return 0
            if args.confirm is None:
                raise ValidationError("hard purge requires --confirm with the exact scope")
            result = hard_purge(
                store, root, args.scope, confirmation=args.confirm, sentinel=args.sentinel,
            )
            _write_json(result)
            return 0 if result.get("status") == "purged" else 2
        if args.command == "profile":
            from .lifecycle import seed_profile
            from .store.service import utc_now
            fields = json.loads(args.input.read_text(encoding="utf-8"))
            store.initialize()
            node_id = seed_profile(
                store, operator_id=_operator_urn(root), fields=fields,
                evidence_ids=args.evidence, valid_from=args.valid_from or utc_now(),
            )
            _write_json({"status": "inferred_profile_seed", "node_id": node_id, "load_bearing": False})
            return 0
        if args.command == "experimental":
            from .lifecycle import feature_status
            _write_json({"status": "ok", "features": feature_status(config)})
            return 0
        if args.command == "retrieve":
            from .retrieve import retrieve_payload
            from .config import effective_selector_mode
            store.initialize()
            selector_mode = effective_selector_mode(config.get("selector"))
            selector_fields: dict[str, object] = {"selector_method": "deterministic"}
            context = None
            exclude_ids: frozenset[str] = frozenset()
            if selector_mode != "deterministic" and args.prompt.strip():
                from .retrieve import select_prompt_payload
                decision = select_prompt_payload(
                    store, root=root, session_id=args.session,
                    state=args.prompt[:SELECTOR_STATE_LIMIT], explicit_domain=args.domain,
                    budget=int(config["context_budget_bytes"]),
                    selector=config["selector"], mode=selector_mode, refresh=args.refresh,
                    local_sections=frozenset({"core", "general", "domain"}),
                )
                selector_fields = decision.fields()
                context = decision.context
                exclude_ids = decision.exclude_ids
                if decision.response is not None:
                    selected = {**decision.response, **selector_fields}
                    if not args.refresh and selected.get("status") == "delivered":
                        _emit_retrieval_json(
                            selected, root=root, session_id=args.session,
                            snapshot_id=str(selected["snapshot_id"]),
                            domain_id=str(selected["receipt_scope"]),
                        )
                    else:
                        _write_json(selected)
                    return 0
            result = retrieve_payload(
                store,
                root=root,
                session_id=args.session,
                prompt=args.prompt,
                explicit_domain=args.domain,
                budget=int(config["context_budget_bytes"]),
                refresh=args.refresh,
                context=context,
                exclude_ids=exclude_ids,
            )
            result = {**result, **selector_fields}
            if not args.refresh and result.get("status") == "delivered":
                _emit_retrieval_json(
                    result, root=root, session_id=args.session,
                    snapshot_id=str(result["snapshot_id"]), domain_id=None,
                )
            else:
                _write_json(result)
            return 0
        if args.command == "health":
            from .health import health_report
            result = health_report(root, store, config)
            _write_json(result)
            return 0 if result.get("status") == "healthy" else 2
        if args.command == "store":
            if args.store_action == "recover":
                _write_json(store.recover())
                return 0
        if args.command == "hook":
            # The bridge terminates a timed-out hook with SIGTERM before
            # escalating to SIGKILL; convert it to SystemExit so open store
            # connections unwind and close instead of orphaning WAL sidecars.
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
            try:
                event = json.load(sys.stdin)
                if not isinstance(event, dict):
                    raise ImprintError("hook event must be an object")
                # JSON can carry a lone surrogate that nothing downstream can encode.
                event = json.loads(json.dumps(event, ensure_ascii=False).encode("utf-8", "replace"))
            except RecursionError:
                # The bridge's own read has a little more stack, so this can get past it.
                _write_json({"hook_schema_version": "1.0.0", "status": "skipped", "reason": "hook_input_invalid"})
                return 0
            native_value = event.get("session_id") or event.get("sessionId")
            native_session = (
                str(native_value) if native_value
                else f"unavailable-event:{uuid.uuid4()}"
            )
            session = _opaque_session_urn(root, native_session)
            if args.action == "session-start":
                _validate_hook_event(event, "SessionStart")
                from .retrieve import retrieve_payload
                store.initialize()
                # SessionStart re-fires on resume/compact — precisely when the
                # context window was wiped and the memory is most needed. Read
                # event.source and refresh (bypass the once-delivery latch) so
                # the payload is redelivered instead of returning already_delivered.
                refresh = str(event.get("source") or "") in _REFRESHING_SESSION_SOURCES
                from .config import effective_selector_mode
                if refresh and effective_selector_mode(config.get("selector")) == "jev":
                    # Jev latches per-turn deliveries for the session; a wiped
                    # context forgets all of them and then takes the ordinary
                    # once-delivery path. Shadow delivers exactly the
                    # deterministic context, so it keeps the refresh contract.
                    # SessionStart never calls Jev.
                    try:
                        from .retrieve import reset_payload_delivery
                        reset_payload_delivery(root=root, session_id=session)
                        refresh = False
                    except Exception:
                        pass  # keep the deterministic refresh contract
                result = retrieve_payload(
                    store, root=root, session_id=session, prompt="",
                    budget=int(config["context_budget_bytes"]), refresh=refresh,
                )
                response = {
                    "hook_schema_version": "1.0.0",
                    "status": result["status"],
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": result.get("payload", ""),
                    },
                }
                if not refresh and result.get("status") == "delivered":
                    _emit_retrieval_json(
                        response, root=root, session_id=session,
                        snapshot_id=str(result["snapshot_id"]), domain_id=None,
                    )
                else:
                    _write_json(response)
                return 0
            if args.action == "user-prompt-submit":
                _validate_hook_event(event, "UserPromptSubmit")
                from .domains import registry_from_config
                from .retrieve import retrieve_payload
                store.initialize()
                prompt = str(event.get("prompt") or event.get("user_prompt") or "")
                selection = registry_from_config(config).select(
                    explicit=str(event["domain_id"]) if event.get("domain_id") else None,
                    path=str(event.get("cwd") or event.get("working_directory") or "") or None,
                    prompt=prompt,
                )
                domain = selection.domain_id
                from .config import effective_selector_mode
                selector_mode = effective_selector_mode(config.get("selector"))
                selector_fields: dict[str, object] = {"selector_method": "deterministic"}
                context = None
                exclude_ids: frozenset[str] = frozenset()
                if selector_mode != "deterministic" and prompt.strip():
                    from .retrieve import select_prompt_payload
                    decision = select_prompt_payload(
                        store, root=root, session_id=session,
                        state=_selector_state(prompt, event, config["selector"]),
                        explicit_domain=domain, budget=int(config["context_budget_bytes"]),
                        selector=config["selector"], mode=selector_mode,
                        deadline=started + HOOK_SELECTOR_BUDGET_SECONDS,
                    )
                    selector_fields = decision.fields()
                    context = decision.context
                    exclude_ids = decision.exclude_ids
                    if decision.response is not None:
                        selected = decision.response
                        response = {
                            "hook_schema_version": "1.0.0",
                            "status": selected["status"],
                            "domain_id": domain,
                            "selection_method": selection.method,
                            **selector_fields,
                            "hookSpecificOutput": {
                                "hookEventName": "UserPromptSubmit",
                                "additionalContext": selected.get("payload", ""),
                            },
                        }
                        if selected.get("status") == "delivered":
                            _emit_retrieval_json(
                                response, root=root, session_id=session,
                                snapshot_id=str(selected["snapshot_id"]),
                                domain_id=str(selected["receipt_scope"]),
                            )
                        else:
                            _write_json(response)
                        return 0
                # Deterministic behavior, and the exact fallback for every
                # selector failure: domain-only injection, or skip.
                if domain is None:
                    _write_json({
                        "hook_schema_version": "1.0.0", "status": "skipped",
                        "reason": selection.diagnostic_code,
                        **selector_fields,
                        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ""},
                    })
                    return 0
                result = retrieve_payload(
                    store, root=root, session_id=session,
                    prompt=prompt, explicit_domain=domain,
                    budget=int(config["context_budget_bytes"]), domain_only=True,
                    context=context, exclude_ids=exclude_ids,
                )
                response = {
                    "hook_schema_version": "1.0.0",
                    "status": result["status"],
                    "domain_id": domain,
                    "selection_method": selection.method,
                    **selector_fields,
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": result.get("payload", ""),
                    },
                }
                if result.get("status") == "delivered":
                    _emit_retrieval_json(
                        response, root=root, session_id=session,
                        snapshot_id=str(result["snapshot_id"]), domain_id=domain,
                    )
                else:
                    _write_json(response)
                return 0
            if args.action == "stop-capture":
                _validate_hook_event(event, "Stop")
                from .capture import build_capture_envelope
                from .capture.schema import OPERATOR_TEXT_OVERSIZED_REASON, text_fits
                from .capture.transcript import (
                    MAX_TRANSCRIPT_BYTES,
                    TAIL_READ_BYTES,
                    TRANSCRIPT_REFUSED_REASON,
                    TRANSCRIPT_TAIL_EXHAUSTED_REASON,
                    TRANSCRIPT_UNAVAILABLE_REASON,
                    TranscriptInFlux,
                    TranscriptUnavailable,
                    _parse_native_stop_snapshot,
                    _read_native_transcript_snapshot,
                )
                from .capture.detector import (
                    detect_explicit_feedback,
                    operator_words,
                    stored_words,
                    strip_embedded_host_content,
                    strip_leading_host_blocks,
                )
                from .capture.provenance import HOST_RECORD_BASIS, UNVERIFIED_BASIS
                from .capture.t3_origin import t3_host_origin
                from .capture.marks import (
                    ALREADY_CAPTURED_REASON,
                    OPERATOR_TURN_EXPIRED_REASON,
                    CapturedTurns,
                    outlived_its_mark,
                    turn_identity,
                )
                operator_text = event.get("operator_text") or event.get("last_user_message")
                prior_assistant = event.get("prior_assistant_output")
                case_description = event.get("case_description")
                contextual_evidence = []
                extensions = {}
                source_entry_id = None
                host_origin = t3_host_origin(config, native_value)
                if not operator_text and isinstance(event.get("transcript_path"), str):
                    # Blocking the stop cannot make the host write a missing or empty
                    # file, or repair a refused one, and the retry fails the same way.
                    # A refused file stays refused: nothing is read from it, and the
                    # skip log is what shows it. Only a file still changing can heal.
                    try:
                        snapshot = _read_native_transcript_snapshot(
                            event["transcript_path"], tail_limit=TAIL_READ_BYTES,
                        )
                        if snapshot.size > MAX_TRANSCRIPT_BYTES:
                            native = _parse_large_native_transcript(
                                event["transcript_path"], snapshot=snapshot, host_origin=host_origin,
                            )
                        else:
                            native = _parse_native_stop_snapshot(snapshot, host_origin=host_origin)
                    except TranscriptInFlux:
                        raise
                    except ValidationError as exc:
                        reason = (
                            TRANSCRIPT_UNAVAILABLE_REASON if isinstance(exc, TranscriptUnavailable)
                            else TRANSCRIPT_REFUSED_REASON
                        )
                        _record_capture_skip(root, reason)
                        _write_json({"hook_schema_version": "1.0.0", "status": "skipped", "reason": reason})
                        return 0
                    if native["skip_reason"]:
                        reason = native["skip_reason"]
                        # What T3's records could not say looks exactly like an
                        # agent's prompt, and nothing else would show that T3
                        # capture has stopped working.
                        if (
                            host_origin is not None and host_origin.skip_reason
                            and native.get("provenance_basis") == UNVERIFIED_BASIS
                        ):
                            reason = host_origin.skip_reason
                            _record_capture_skip(root, reason)
                        elif reason == TRANSCRIPT_TAIL_EXHAUSTED_REASON:
                            _record_capture_skip(root, reason)
                        skipped = {
                            "hook_schema_version": "1.0.0", "status": "skipped",
                            "reason": reason,
                        }
                        # Which tell dropped the entry, so marker-basis drops
                        # stay distinguishable from declared-provenance ones.
                        if native.get("provenance_basis"):
                            skipped["provenance_basis"] = native["provenance_basis"]
                        _write_json(skipped)
                        return 0
                    if outlived_its_mark(native.get("source_timestamp")):
                        _write_json({
                            "hook_schema_version": "1.0.0", "status": "skipped",
                            "reason": OPERATOR_TURN_EXPIRED_REASON,
                        })
                        return 0
                    if native.get("degradation"):
                        extensions["org.imprint.transcript"] = native["degradation"]
                    operator_text = native["operator_text"]
                    if native["provenance_basis"] == HOST_RECORD_BASIS and host_origin is not None:
                        attested = host_origin.evidence(operator_text)
                        if attested:
                            extensions["org.imprint.host_origin"] = {"schema_version": "1.0.0", "payload": attested}
                        operator_text = operator_words(host_origin.recorded_words(operator_text))
                    prior_assistant = native["prior_assistant_output"]
                    case_description = native["case_description"]
                    source_entry_id = native.get("source_entry_id")
                    if isinstance(prior_assistant, str) and prior_assistant.strip():
                        contextual_evidence = [{
                            "kind": "context", "content": prior_assistant,
                            "source_locator": native["source_locator"],
                        }]
                if not isinstance(operator_text, str) or not operator_text.strip():
                    _write_json({"hook_schema_version": "1.0.0", "status": "skipped", "reason": "feedback_text_unavailable"})
                    return 0
                # Detection keeps the pastes in place: the line after a quote is
                # the operator's, the same line after a bare colon is not.
                spoken_text = strip_embedded_host_content(strip_leading_host_blocks(operator_text))
                operator_text = stored_words(spoken_text)
                if not operator_text:
                    _write_json({"hook_schema_version": "1.0.0", "status": "skipped", "reason": "not_explicit_feedback"})
                    return 0
                detection = detect_explicit_feedback(
                    spoken_text,
                    prior_operator_text=event.get("prior_operator_text"),
                    prior_assistant_output=prior_assistant,
                )
                if not detection.is_feedback:
                    _write_json({"hook_schema_version": "1.0.0", "status": "skipped", "reason": "not_explicit_feedback"})
                    return 0
                # A verbatim record is never cut to fit, and blocking the stop cannot
                # make the text any shorter, so this turn is skipped and says so.
                if not text_fits(operator_text):
                    _record_capture_skip(root, OPERATOR_TEXT_OVERSIZED_REASON)
                    _write_json({
                        "hook_schema_version": "1.0.0", "status": "skipped",
                        "reason": OPERATOR_TEXT_OVERSIZED_REASON,
                    })
                    return 0
                # Stop fires on every assistant turn, so the same trailing
                # operator utterance is re-read until the operator speaks again.
                # Claim the turn once per session or the Verdict is duplicated.
                marks = CapturedTurns(root)
                turn_id = turn_identity(source_entry_id, operator_text)
                # With no session id every event gets a session of its own, which
                # no mark would survive, but the transcript still names the session.
                mark_scope = session
                if not native_value and isinstance(event.get("transcript_path"), str):
                    mark_scope = _opaque_session_urn(root, f"transcript:{event['transcript_path']}")
                if not marks.claim(mark_scope, turn_id):
                    _write_json({
                        "hook_schema_version": "1.0.0", "status": "skipped",
                        "reason": ALREADY_CAPTURED_REASON,
                    })
                    return 0
                # The claim is taken before the write so two concurrent Stops
                # cannot both spool the turn, which makes an unwind mandatory:
                # a mark left over a failed capture would make the healed retry
                # skip the turn as already captured and lose it for good.
                # Invariant: mark lifetime == envelope durability. The claim is
                # given back only while nothing is spooled; once the envelope is
                # durable the mark is kept even on failure, because the turn IS
                # captured and a later compile heals from the surviving file.
                envelope_path = None
                try:
                    envelope = build_capture_envelope(
                        operator_id=_operator_urn(root), session_id=session,
                        node_id=str(config.get("node_id", "primary")),
                        case_description=str(case_description or "Explicit operator feedback witnessed by explicit hook input"),
                        raw_operator_text=operator_text, call_type=detection.call_type,
                        capture_mechanism="claude_code_stop_hook", captured_by="imprint-hook",
                        reason=event.get("reason") if isinstance(event.get("reason"), str) else None,
                        contextual_evidence=contextual_evidence,
                        extensions=extensions,
                    )
                    envelope_path = write_envelope(root, envelope)
                    receipt = {
                        "hook_schema_version": "1.0.0", "status": "queued",
                        "event_id": envelope["input_event_id"], "spool_file": envelope_path.name,
                        "canonical_status": "spool_only",
                    }
                    if bool(config.get("compiler")):
                        from .compiler import acknowledgement_committed
                        counts = compile_spools(
                            root, store, compiler_authorized=True,
                        )
                        if not acknowledgement_committed(root, envelope_path, envelope):
                            raise ImprintError("Stop capture lacks exact durable canonical acknowledgement")
                        receipt["canonical_status"] = "compiled"
                        receipt["compile"] = counts
                        receipt["compile_status"] = (
                            "degraded" if counts["quarantined"] else "healthy"
                        )
                        if counts["quarantined"]:
                            receipt["unrelated_quarantine_count"] = counts["quarantined"]
                except BaseException:
                    if envelope_path is None and not marks.release(mark_scope, turn_id):
                        # A stuck mark silently costs the next retry of this turn,
                        # so the failure receipt has to say so.
                        error_details["mark_release_failed"] = True
                    raise
                if "org.imprint.transcript" in extensions:
                    receipt["degradation"] = extensions["org.imprint.transcript"]["payload"]
                _write_json(receipt)
                return 0
            if args.action == "health-check":
                _validate_hook_event(event, "SessionStart")
                from .health import health_report
                result = health_report(root, store, config)
                _write_json({"hook_schema_version": "1.0.0", **result})
                return 0 if result.get("status") == "healthy" else 2
    except (ImprintError, OSError, json.JSONDecodeError) as exc:
        _write_json({
            "status": "error", "error": str(exc), "error_type": type(exc).__name__,
            **error_details,
        })
        return 2
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
