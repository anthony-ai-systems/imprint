"""Deterministic canonical writer. Models never receive this authority."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from imprint.constants import ONTOLOGY_SCHEMA_VERSION, PRODUCT_VERSION, STORE_SCHEMA_VERSION
from imprint.errors import ConflictError, ValidationError
from imprint.capture.schema import validate_capture_envelope
from imprint.ontology.contracts import validate_node_contract, validate_relation_contract
from imprint.ontology.references import validate_payload_references
from imprint.ontology.schema import canonical_bytes, make_urn, payload_sha256, require_urn
from imprint.permissions import secure_directory, secure_file
from .schema import SCHEMA_SQL


DERIVED_NODE_TYPES = frozenset({
    "Principle", "Belief", "Value", "Rule", "Pattern", "Domain", "FeedbackProfile", "Proposal",
})


def _secure_sqlite_state(path: Path) -> None:
    """Tighten the database and every extant SQLite private-state sidecar."""
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ):
        if candidate.exists():
            secure_file(candidate)

def version_provenance(*, status: str, authority_tier: str, actor_class: str,
                       actor_id: str, mechanism: str, event_id: str,
                       model: str | None = None, prompt_recipe: str | None = None,
                       proposal_id: str | None = None, ratifier: str | None = None,
                       relation: str | None = None) -> dict[str, Any]:
    return {
        "provenance_schema_version": "1.0.0", "status": status,
        "authority_tier": authority_tier, "actor_class": actor_class,
        "actor_id": actor_id, "mechanism": mechanism,
        "software": {"name": "imprint-local", "version": PRODUCT_VERSION},
        "model": model, "prompt_recipe": prompt_recipe,
        "proposal_id": proposal_id, "ratifier": ratifier,
        "event_id": event_id, "relation": relation,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ImprintStore:
    def __init__(self, path: Path | str, *, expected_operator_id: str | None = None,
                 expected_node_id: str | None = None):
        self.path = Path(path)
        self.expected_operator_id = expected_operator_id
        self.expected_node_id = expected_node_id
        self._compatibility_verified = False

    def _require_configured_operator(self, operator_id: str) -> None:
        if self.expected_operator_id is not None and operator_id != self.expected_operator_id:
            raise ValidationError("operator does not match configured identity")

    def initialize(self) -> None:
        existing = self.path.exists()
        secure_directory(self.path.parent)
        try:
            if existing:
                context = self.connect()
            else:
                @contextmanager
                def new_store_connection():
                    conn = sqlite3.connect(self.path, timeout=30)
                    try:
                        _secure_sqlite_state(self.path)
                        yield conn
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        conn.close()
                        _secure_sqlite_state(self.path)
                context = new_store_connection()
            with context as conn:
                conn.executescript(SCHEMA_SQL)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key,value) VALUES('store_schema_version',?)",
                    (STORE_SCHEMA_VERSION,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key,value) VALUES('ontology_schema_version',?)",
                    (ONTOLOGY_SCHEMA_VERSION,),
                )
            _secure_sqlite_state(self.path)
            self._compatibility_verified = True
        except Exception:
            self._compatibility_verified = False
            raise

    @staticmethod
    def _require_connection_compatible(
        conn: sqlite3.Connection, *,
        store_versions: frozenset[str] = frozenset({STORE_SCHEMA_VERSION}),
        ontology_versions: frozenset[str] | None = frozenset({ONTOLOGY_SCHEMA_VERSION}),
    ) -> None:
        """Validate the exact open database handle before any mutable PRAGMA or DDL."""
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if table is None:
            raise ValidationError("existing store is missing schema metadata")
        row = conn.execute(
            "SELECT value FROM meta WHERE key='store_schema_version'"
        ).fetchone()
        if row is None:
            raise ValidationError("existing store is missing store_schema_version")
        if not isinstance(row[0], str) or row[0] not in store_versions:
            raise ValidationError(
                f"incompatible store schema {row[0]!r}; expected one of {sorted(store_versions)}"
            )
        ontology = conn.execute(
            "SELECT value FROM meta WHERE key='ontology_schema_version'"
        ).fetchone()
        if ontology is None:
            raise ValidationError("existing store is missing ontology_schema_version")
        if (
            not isinstance(ontology[0], str)
            or (ontology_versions is not None and ontology[0] not in ontology_versions)
        ):
            raise ValidationError(
                f"incompatible ontology schema {ontology[0]!r}"
            )

    def _require_existing_store_compatible(
        self, *,
        store_versions: frozenset[str] = frozenset({STORE_SCHEMA_VERSION}),
        ontology_versions: frozenset[str] | None = frozenset({ONTOLOGY_SCHEMA_VERSION}),
    ) -> tuple[int, int]:
        """Inspect an existing database, healing stale WAL residue in passing.

        The immutable read below is only sound when no writer holds the store,
        so WAL/SHM sidecars must be gone before it runs. Sidecars left by a
        hard-killed writer used to brick every subsequent open until a manual
        ``store recover``; now they are drained in place first, and only a
        store genuinely held by a live connection keeps refusing.
        """
        try:
            resolved = self.path.resolve(strict=True)
            if not resolved.is_file() or self.path.is_symlink():
                raise ValidationError("store path must be a regular non-symlink file")
            before = resolved.stat(follow_symlinks=False)
            sidecars = tuple(
                Path(str(resolved) + suffix) for suffix in ("-wal", "-shm")
            )
            if any(path.exists() for path in sidecars):
                if not self._drain_stale_sidecars(resolved):
                    raise ValidationError(
                        "store has WAL/SHM sidecars; close the active writer or recover the store before use"
                    )
            uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                self._require_connection_compatible(
                    conn, store_versions=store_versions,
                    ontology_versions=ontology_versions,
                )
            finally:
                conn.close()
            after = resolved.stat(follow_symlinks=False)
            identity = (before.st_dev, before.st_ino)
            if identity != (after.st_dev, after.st_ino):
                raise ValidationError("store path changed during compatibility validation")
            return identity
        except ValidationError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ValidationError("existing store is corrupt or unreadable") from exc

    @contextmanager
    def connect(self):
        if not self.path.exists():
            raise ValidationError("store must be initialized before use")
        identity = self._require_existing_store_compatible()
        try:
            resolved = self.path.resolve(strict=True)
            conn = sqlite3.connect(
                f"{resolved.as_uri()}?mode=rw", uri=True, timeout=30,
            )
            _secure_sqlite_state(resolved)
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ValidationError("store changed before connection") from exc
        handle_validated = False
        try:
            self._require_connection_compatible(conn)
            current = self.path.resolve(strict=True).stat(follow_symlinks=False)
            if identity != (current.st_dev, current.st_ino):
                raise ValidationError("store path changed before connection")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            handle_validated = True
            yield conn
            committed_path = self.path.resolve(strict=True).stat(follow_symlinks=False)
            if identity != (committed_path.st_dev, committed_path.st_ino):
                raise ValidationError("store path changed before commit")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            if not handle_validated:
                for sidecar in (
                    Path(str(resolved) + "-wal"), Path(str(resolved) + "-shm"),
                    Path(str(resolved) + "-journal"),
                ):
                    sidecar.unlink(missing_ok=True)
            if self.path.exists():
                current_path = self.path.resolve(strict=True)
                current = current_path.stat(follow_symlinks=False)
                if identity == (current.st_dev, current.st_ino):
                    _secure_sqlite_state(current_path)

    @contextmanager
    def _migration_connection(
        self, *, store_versions: frozenset[str],
        ontology_versions: frozenset[str] | None = frozenset({ONTOLOGY_SCHEMA_VERSION}),
    ):
        """Restricted compatibility exception for explicit migration code only.

        Canonical writers never call this path; their ordinary ``connect``
        remains pinned to the current store and ontology contracts.
        """
        if not store_versions or any(not isinstance(item, str) or not item for item in store_versions):
            raise ValidationError("migration store versions are invalid")
        identity = self._require_existing_store_compatible(
            store_versions=store_versions, ontology_versions=ontology_versions,
        )
        resolved = self.path.resolve(strict=True)
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=rw", uri=True, timeout=30)
        handle_validated = False
        try:
            _secure_sqlite_state(resolved)
            self._require_connection_compatible(
                conn, store_versions=store_versions,
                ontology_versions=ontology_versions,
            )
            current = self.path.resolve(strict=True).stat(follow_symlinks=False)
            if identity != (current.st_dev, current.st_ino):
                raise ValidationError("store path changed before migration connection")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            handle_validated = True
            yield conn
            committed_path = self.path.resolve(strict=True).stat(follow_symlinks=False)
            if identity != (committed_path.st_dev, committed_path.st_ino):
                raise ValidationError("store path changed before migration commit")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            if not handle_validated:
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(resolved) + suffix).unlink(missing_ok=True)
            if self.path.exists():
                current_path = self.path.resolve(strict=True)
                current = current_path.stat(follow_symlinks=False)
                if identity == (current.st_dev, current.st_ino):
                    _secure_sqlite_state(current_path)

    def integrity_check(self) -> str:
        with self.connect() as conn:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])

    _WAL_MAGIC = (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")

    def _drain_stale_sidecars(self, resolved: Path, *, force: bool = False) -> bool:
        """Replay orphaned WAL crash residue in place; True when the store is clean.

        Proves no live connection holds the store (zero busy grace, so a held
        store is reported rather than waited on), lets SQLite checkpoint the
        committed WAL frames into the main database, then drops the drained
        sidecars. Returns False when a live connection holds the store or when
        the ``-wal`` file is not something SQLite can replay (unknown bytes are
        never silently discarded; ``force`` lifts that gate for the explicit
        ``store recover`` command); propagates ``OSError``/
        ``sqlite3.DatabaseError`` when the store itself is unreadable and
        ``ValidationError`` when it is incompatible.
        """
        wal = Path(str(resolved) + "-wal")
        if not force and wal.exists() and wal.stat().st_size > 0:
            with wal.open("rb") as handle:
                if handle.read(4) not in self._WAL_MAGIC:
                    return False
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=rw", uri=True, timeout=0)
        try:
            self._require_connection_compatible(conn)
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.OperationalError:
                return False
            busy = int(row[0]) if row and row[0] is not None else 1
            if busy != 0:
                return False
        finally:
            conn.close()
        # The committed frames now live in the main database, so removing the
        # drained sidecars is safe. Closing the last connection usually removes
        # them already; the unlinks mop up what a crashed reader left behind.
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(resolved) + suffix).unlink(missing_ok=True)
        _secure_sqlite_state(resolved)
        return True

    def recover(self) -> dict[str, Any]:
        """Replay orphaned WAL crash residue into the store, or report a live writer.

        Ordinary opens heal stale residue automatically, but they surface a held
        store as the generic sidecar refusal. This explicit command exists for
        operators: it names the live-writer case precisely and gives a positive
        confirmation that the store is clean. A store held by a live writer is
        left untouched.
        """
        if not self.path.exists():
            raise ValidationError("store must be initialized before recovery")
        try:
            resolved = self.path.resolve(strict=True)
            if not resolved.is_file() or self.path.is_symlink():
                raise ValidationError("store path must be a regular non-symlink file")
        except OSError as exc:
            raise ValidationError("existing store is corrupt or unreadable") from exc
        sidecars = tuple(
            Path(str(resolved) + suffix) for suffix in ("-wal", "-shm", "-journal")
        )
        if not any(path.exists() for path in sidecars):
            # No crash residue; confirm the store is still a compatible imprint database.
            self._require_existing_store_compatible()
            return {"status": "clean"}
        try:
            drained = self._drain_stale_sidecars(resolved, force=True)
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ValidationError("existing store is corrupt or unreadable") from exc
        if not drained:
            raise ValidationError(
                "store is held by a live writer; close the active session before recovery"
            )
        # Prove the recovered store now opens through the ordinary path.
        self._require_existing_store_compatible()
        return {"status": "recovered"}

    def apply_capture(self, envelope: dict[str, Any], *, source_path: str = "direct") -> str:
        validate_capture_envelope(envelope)
        if self.expected_operator_id is not None and envelope["operator_id"] != self.expected_operator_id:
            raise ValidationError("capture operator does not match the configured canonical operator")
        if self.expected_node_id is not None and envelope["node_id"] != self.expected_node_id:
            raise ValidationError("capture node does not match the configured producer node")
        event_id = envelope["input_event_id"]
        event_hash = payload_sha256(envelope)
        now = utc_now()
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute(
                    "SELECT payload_sha256 FROM consumed_inputs WHERE input_event_id=?", (event_id,)
                ).fetchone()
                if prior:
                    if prior[0] == event_hash:
                        return "duplicate"
                    raise ConflictError("same input_event_id has different bytes")
                conn.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, "captured", envelope["operator_id"], now, envelope["captured_at"],
                     canonical_bytes(envelope).decode(), event_hash, None, "captured"),
                )
                case = envelope["case"]
                verdict = envelope["verdict"]
                call = verdict["call"]
                self._insert_node(conn, case["case_id"], "Case", case, envelope, event_id, now)
                self._insert_node(conn, verdict["verdict_id"], "Verdict", verdict, envelope, event_id, now)
                self._insert_node(conn, call["call_id"], "Call", call, envelope, event_id, now)
                self._insert_edge(conn, "verdict_about_case", verdict["verdict_id"], case["case_id"], envelope, event_id, now)
                self._insert_edge(conn, "made_call", verdict["verdict_id"], call["call_id"], envelope, event_id, now)
                for evidence in envelope["evidence"]:
                    self._insert_node(conn, evidence["evidence_id"], "Evidence", evidence, envelope, event_id, now)
                    self._insert_edge(conn, "supported_by", verdict["verdict_id"], evidence["evidence_id"], envelope, event_id, now)
                    conn.execute(
                        "INSERT INTO source_receipts VALUES(?,?,?,?,?)",
                        (evidence["evidence_id"], evidence.get("kind", "operator_verbatim"),
                         evidence.get("source_locator", ""), evidence["content_sha256"], event_id),
                    )
                alternatives = {item["alternative_id"]: item for item in envelope.get("alternatives", [])}
                for alt_id, alternative in alternatives.items():
                    self._insert_node(conn, alt_id, "Alternative", alternative, envelope, event_id, now)
                for alt_id in verdict.get("chosen_alternative_ids", []):
                    self._insert_edge(conn, "chose_alternative", verdict["verdict_id"], alt_id, envelope, event_id, now)
                for alt_id in verdict.get("rejected_alternative_ids", []):
                    self._insert_edge(conn, "rejected_alternative", verdict["verdict_id"], alt_id, envelope, event_id, now)
                conn.execute(
                    "INSERT INTO consumed_inputs VALUES(?,?,?,?)", (event_id, event_hash, now, source_path)
                )
        except sqlite3.IntegrityError as exc:
            # A constraint violation is content that was read and rejected, not
            # infrastructure failure. Raising a domain conflict lets callers quarantine
            # this one input instead of halting. connect() has already rolled back.
            raise ConflictError("capture conflicts with committed store state") from exc
        return "captured"

    def _insert_node(self, conn, node_id, node_type, payload, envelope, event_id, now):
        conn.execute("INSERT INTO nodes VALUES(?,?,?,?)", (node_id, node_type, envelope["operator_id"], event_id))
        version_id = make_urn("node-version")
        evidence_ids = [item["evidence_id"] for item in envelope.get("evidence", [])]
        conn.execute(
            "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, node_id, canonical_bytes(payload).decode(), payload_sha256(payload), "captured",
             "captured_judgment", canonical_bytes(version_provenance(
                 status="captured", authority_tier="captured_judgment", actor_class="operator",
                 actor_id=envelope["operator_id"], mechanism=envelope["capture_mechanism"], event_id=event_id,
             )).decode(), json.dumps(evidence_ids), envelope["captured_at"], None,
             now, None, event_id, None),
        )

    def _insert_edge(self, conn, edge_type, source_id, target_id, envelope, event_id, now):
        edge_id = make_urn("edge")
        payload = {"why": "witnessed in raw capture", "relation": edge_type}
        conn.execute(
            "INSERT INTO edges VALUES(?,?,?,?,?,?)",
            (edge_id, edge_type, source_id, target_id, envelope["operator_id"], event_id),
        )
        conn.execute(
            "INSERT INTO edge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (make_urn("edge-version"), edge_id, canonical_bytes(payload).decode(), payload_sha256(payload),
             "captured", "captured_judgment", canonical_bytes(version_provenance(
                 status="captured", authority_tier="captured_judgment", actor_class="operator",
                 actor_id=envelope["operator_id"], mechanism=envelope["capture_mechanism"], event_id=event_id,
                 relation=edge_type,
             )).decode(), json.dumps([x["evidence_id"] for x in envelope["evidence"]]),
             envelope["captured_at"], None, now, None, event_id, None),
        )

    def current_nodes(self, types: Iterable[str] | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = "WHERE nv.system_to IS NULL"
        if types:
            values = list(types)
            where += f" AND n.node_type IN ({','.join('?' for _ in values)})"
            params.extend(values)
        query = f"""
          SELECT n.node_id,n.node_type,n.operator_id,nv.payload_json,nv.payload_sha256,
                 nv.provenance_status,nv.authority_tier,nv.provenance_json,nv.evidence_json,nv.valid_from,nv.valid_to,
                 nv.system_from,nv.system_to,nv.event_id
          FROM nodes n JOIN node_versions nv USING(node_id) {where}
          ORDER BY n.node_type,n.node_id
        """
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["provenance"] = json.loads(item.pop("provenance_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    @staticmethod
    def _domain_node_id(operator_id: str, domain_id: str) -> str:
        value = uuid.uuid5(uuid.NAMESPACE_URL, f"imprint-domain:{operator_id}:{domain_id}")
        return f"urn:imprint:domain:{value}"

    @staticmethod
    def _require_actor(actor_id: str) -> str:
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ValidationError("actor identity is required")
        return actor_id.strip()

    @staticmethod
    def _require_evidence(conn, evidence_ids: Iterable[str]) -> list[str]:
        values = list(dict.fromkeys(evidence_ids))
        if not values:
            raise ValidationError("at least one canonical evidence reference is required")
        for evidence_id in values:
            known = conn.execute(
                "SELECT 1 FROM nodes WHERE node_id=? AND node_type='Evidence'", (evidence_id,)
            ).fetchone()
            known = known or conn.execute(
                "SELECT 1 FROM source_receipts WHERE source_id=?", (evidence_id,)
            ).fetchone()
            if not known:
                raise ValidationError("evidence must exist in canonical Evidence or source receipts")
        return values

    def add_domain(
        self, *, domain_id: str, public_label: str, description: str,
        evidence_ids: list[str], operator_id: str, actor_id: str,
        valid_from: str | None = None,
    ) -> str:
        """Create one canonical, operator-declared Domain with a stable local ID."""
        if self.expected_operator_id is not None and operator_id != self.expected_operator_id:
            raise ValidationError("domain operator does not match configured identity")
        if not isinstance(domain_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", domain_id):
            raise ValidationError("domain_id must be a safe lowercase identifier")
        if not public_label.strip() or not description.strip():
            raise ValidationError("domain public_label and description are required")
        actor_id = self._require_actor(actor_id)
        node_id = self._domain_node_id(operator_id, domain_id)
        event_id = make_urn("event")
        now = utc_now()
        valid_from = valid_from or now
        payload = {
            "domain_id": domain_id, "public_label": public_label.strip(),
            "description": description.strip(), "selected": False, "frozen": False,
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            evidence_ids = self._require_evidence(conn, evidence_ids)
            if conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone():
                raise ConflictError("domain already exists")
            event_payload = {"node_id": node_id, "payload": payload, "evidence_ids": evidence_ids, "actor_id": actor_id}
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "domain_added", operator_id, now, valid_from,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload), None, "captured"),
            )
            conn.execute("INSERT INTO nodes VALUES(?,?,?,?)", (node_id, "Domain", operator_id, event_id))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), node_id, canonical_bytes(payload).decode(), payload_sha256(payload),
                 "captured", "captured_judgment", canonical_bytes(version_provenance(
                     status="captured", authority_tier="captured_judgment", actor_class="operator",
                     actor_id=actor_id, mechanism="explicit_domain_add", event_id=event_id,
                 )).decode(), json.dumps(evidence_ids), valid_from, None, now, None, event_id, None),
            )
        return node_id

    def list_domains(self) -> list[dict[str, Any]]:
        return self.current_nodes(["Domain"])

    def _change_domain_state(self, domain_id: str, *, actor_id: str, action: str) -> str:
        actor_id = self._require_actor(actor_id)
        if action not in {"select", "freeze"}:
            raise ValidationError("unsupported domain state transition")
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
              SELECT n.node_id,n.operator_id,nv.* FROM nodes n JOIN node_versions nv USING(node_id)
              WHERE n.node_type='Domain' AND nv.system_to IS NULL ORDER BY n.node_id
            """).fetchall()
            selected = None
            for row in rows:
                payload = json.loads(row["payload_json"])
                if payload.get("domain_id") == domain_id:
                    selected = row
                    break
            if selected is None:
                raise ValidationError("canonical Domain does not exist")
            selected_payload = json.loads(selected["payload_json"])
            if action == "freeze" and selected_payload.get("frozen") is True:
                raise ConflictError("domain is already frozen")
            event_payload = {"domain_id": domain_id, "node_id": selected["node_id"], "actor_id": actor_id, "action": action}
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, {"select": "domain_selected", "freeze": "domain_frozen"}[action],
                 selected["operator_id"], now, now,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload), selected["event_id"], "captured"),
            )
            affected = rows if action == "select" else [selected]
            for row in affected:
                payload = json.loads(row["payload_json"])
                new_payload = dict(payload)
                if action == "select":
                    new_payload["selected"] = row["node_id"] == selected["node_id"]
                else:
                    new_payload["frozen"] = True
                if new_payload == payload:
                    continue
                conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, row["version_id"]))
                conn.execute(
                    "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (make_urn("node-version"), row["node_id"], canonical_bytes(new_payload).decode(),
                     payload_sha256(new_payload), "captured", "captured_judgment",
                     canonical_bytes(version_provenance(
                         status="captured", authority_tier="captured_judgment", actor_class="operator",
                         actor_id=actor_id, mechanism=f"explicit_domain_{action}", event_id=event_id,
                     )).decode(), row["evidence_json"], row["valid_from"], row["valid_to"],
                     now, None, event_id, row["version_id"]),
                )
        return event_id

    def select_domain(self, domain_id: str, *, actor_id: str) -> str:
        return self._change_domain_state(domain_id, actor_id=actor_id, action="select")

    def freeze_domain(self, domain_id: str, *, actor_id: str) -> str:
        return self._change_domain_state(domain_id, actor_id=actor_id, action="freeze")

    def add_transition(
        self, relation: str, source_id: str, target_id: str, *, reason: str,
        evidence_ids: list[str], actor_id: str,
    ) -> str:
        """Append an explicit contradiction or supersession without erasing history."""
        if relation not in {"contradicts", "supersedes"}:
            raise ValidationError("relation must be contradicts or supersedes")
        if source_id == target_id:
            raise ValidationError("transition endpoints must be different")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("transition reason is required")
        actor_id = self._require_actor(actor_id)
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            evidence_ids = self._require_evidence(conn, evidence_ids)
            endpoints = conn.execute("""
              SELECT n.node_id,n.node_type,n.operator_id,nv.version_id,nv.event_id
              FROM nodes n JOIN node_versions nv USING(node_id)
              WHERE n.node_id IN (?,?) AND nv.system_to IS NULL
            """, (source_id, target_id)).fetchall()
            by_id = {row["node_id"]: row for row in endpoints}
            if set(by_id) != {source_id, target_id}:
                raise ValidationError("both transition endpoints must be current canonical nodes")
            if relation == "supersedes" and by_id[source_id]["node_type"] != by_id[target_id]["node_type"]:
                raise ValidationError("supersession endpoints must have the same node type")
            operator_id = by_id[source_id]["operator_id"]
            if by_id[target_id]["operator_id"] != operator_id:
                raise ValidationError("transition endpoints must belong to the same operator")
            if self.expected_operator_id is not None and operator_id != self.expected_operator_id:
                raise ValidationError("transition operator does not match configured identity")
            payload = {
                "relation": relation, "source_id": source_id, "target_id": target_id,
                "reason": reason.strip(), "evidence_ids": evidence_ids, "actor_id": actor_id,
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, relation, operator_id, now, now, canonical_bytes(payload).decode(),
                 payload_sha256(payload), by_id[source_id]["event_id"], "captured"),
            )
            edge_id = make_urn("edge")
            edge_payload = {"relation": relation, "reason": reason.strip()}
            conn.execute("INSERT INTO edges VALUES(?,?,?,?,?,?)", (edge_id, relation, source_id, target_id, operator_id, event_id))
            conn.execute(
                "INSERT INTO edge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("edge-version"), edge_id, canonical_bytes(edge_payload).decode(),
                 payload_sha256(edge_payload), "captured", "captured_judgment",
                 canonical_bytes(version_provenance(
                     status="captured", authority_tier="captured_judgment", actor_class="operator",
                     actor_id=actor_id, mechanism="explicit_transition", event_id=event_id, relation=relation,
                 )).decode(), json.dumps(evidence_ids), now, None, now, None, event_id, None),
            )
            if relation == "supersedes":
                conn.execute(
                    "UPDATE node_versions SET valid_to=?,system_to=? WHERE version_id=?",
                    (now, now, by_id[target_id]["version_id"]),
                )
                conn.execute("""
                  UPDATE edge_versions SET system_to=? WHERE edge_id IN (
                    SELECT edge_id FROM edges WHERE (source_id=? OR target_id=?) AND edge_id<>?
                  ) AND system_to IS NULL
                """, (now, target_id, target_id, edge_id))
        return edge_id

    def append_derived_node(
        self,
        *,
        node_type: str,
        payload: dict[str, Any],
        provenance_status: str,
        authority_tier: str,
        evidence_ids: list[str],
        operator_id: str,
        valid_from: str,
        proposed_by: str,
        model: str | None = None,
        prompt_recipe: str | None = None,
    ) -> str:
        """Append an extracted or inferred object after deterministic validation."""
        if self.expected_operator_id is not None and operator_id != self.expected_operator_id:
            raise ValidationError("derived operator does not match configured identity")
        if provenance_status not in {"extracted", "inferred"}:
            raise ValidationError("derived append accepts only extracted or inferred")
        if provenance_status == "extracted" and not evidence_ids:
            raise ValidationError("extracted objects require evidence")
        if node_type == "Pattern" and len(set(evidence_ids)) < 2:
            raise ValidationError("Pattern requires evidence from at least two cases")
        if node_type not in DERIVED_NODE_TYPES:
            raise ValidationError("unsupported derived ontology node type")
        allowed_tiers = {"inferred": {"inferred_candidate", "observed_candidate"}, "extracted": {"imported_floor", "observed_candidate"}}
        if authority_tier not in allowed_tiers[provenance_status]:
            raise ValidationError("authority tier is incompatible with provenance status")
        if not proposed_by or not proposed_by.strip():
            raise ValidationError("proposed_by is required")
        try:
            parsed_valid_from = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValidationError("valid_from must be an RFC3339 timestamp") from exc
        if parsed_valid_from.tzinfo is None:
            raise ValidationError("valid_from must include timezone")
        node_id = make_urn(node_type.lower())
        event_id = make_urn("event")
        now = utc_now()
        event_payload = {
            "node_id": node_id,
            "node_type": node_type,
            "payload": payload,
            "evidence_ids": evidence_ids,
            "proposed_by": proposed_by,
            "authority_tier": authority_tier,
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for evidence_id in set(evidence_ids):
                known = conn.execute("SELECT 1 FROM nodes WHERE node_id=? AND node_type='Evidence'", (evidence_id,)).fetchone()
                known = known or conn.execute("SELECT 1 FROM source_receipts WHERE source_id=?", (evidence_id,)).fetchone()
                if not known:
                    raise ValidationError("derived evidence must exist in canonical Evidence or source receipts")
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, provenance_status, operator_id, now, valid_from,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload), None,
                 provenance_status),
            )
            conn.execute("INSERT INTO nodes VALUES(?,?,?,?)", (node_id, node_type, operator_id, event_id))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), node_id, canonical_bytes(payload).decode(), payload_sha256(payload),
                 provenance_status, authority_tier, canonical_bytes(version_provenance(
                     status=provenance_status, authority_tier=authority_tier,
                     actor_class="model" if model else "software", actor_id=proposed_by,
                     mechanism="validated_proposal", event_id=event_id, model=model,
                     prompt_recipe=prompt_recipe, proposal_id=event_id,
                 )).decode(), json.dumps(evidence_ids), valid_from, None,
                 now, None, event_id, None),
            )
        return node_id

    def append_semantic_node(self, contract: dict[str, Any], *, valid_from: str) -> str:
        """Persist one fully typed semantic contract through the canonical writer."""
        value = validate_node_contract(contract)
        try:
            parsed_valid_from = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValidationError("valid_from must be an RFC3339 timestamp") from exc
        if parsed_valid_from.tzinfo is None:
            raise ValidationError("valid_from must include timezone")
        operator_id = value["operator_id"]
        if self.expected_operator_id is not None and operator_id != self.expected_operator_id:
            raise ValidationError("semantic node operator does not match configured identity")
        node_id = value["node_id"]
        node_type = value["node_type"]
        if node_type == "DirectionScore":
            raise ValidationError("DirectionScore is an analytical comparison and cannot be persisted")
        payload = value["payload"]
        provenance = value["provenance"]
        evidence_ids = provenance["evidence_ids"]
        event_id = make_urn("event")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone():
                raise ConflictError("semantic node already exists")
            if evidence_ids:
                self._require_evidence(conn, evidence_ids)

            def node_lookup(identifier: str) -> tuple[str, str] | None:
                row = conn.execute(
                    "SELECT node_type,operator_id FROM nodes WHERE node_id=?",
                    (identifier,),
                ).fetchone()
                if row:
                    return row["node_type"], row["operator_id"]
                receipt = conn.execute(
                    """SELECT e.operator_id FROM source_receipts sr
                       JOIN events e USING(event_id) WHERE sr.source_id=?""",
                    (identifier,),
                ).fetchone()
                return ("Evidence", receipt["operator_id"]) if receipt else None

            def version_lookup(identifier: str) -> tuple[str, str] | None:
                row = conn.execute(
                    """SELECT nv.node_id,n.operator_id FROM node_versions nv
                       JOIN nodes n USING(node_id) WHERE nv.version_id=?""",
                    (identifier,),
                ).fetchone()
                return (row["node_id"], row["operator_id"]) if row else None

            validate_payload_references(
                node_type,
                payload,
                operator_id=operator_id,
                provenance_evidence_ids=evidence_ids,
                node_lookup=node_lookup,
                version_lookup=version_lookup,
            )
            consent_grant_id = payload.get("consent_grant_id") if isinstance(payload, dict) else None
            if consent_grant_id is not None:
                grant_row = conn.execute("""
                  SELECT n.operator_id,nv.payload_json FROM nodes n JOIN node_versions nv USING(node_id)
                  WHERE n.node_id=? AND n.node_type='ConsentGrant' AND nv.system_to IS NULL
                """, (consent_grant_id,)).fetchone()
                if not grant_row or grant_row["operator_id"] != operator_id:
                    raise ValidationError("semantic observation requires a current same-operator ConsentGrant")
                from imprint.ontology.operator import consent_authorizes
                purpose = "outcome_learning" if node_type == "Outcome" else "behavioral_observation"
                if not consent_authorizes(
                    json.loads(grant_row["payload_json"]),
                    source_class=payload["source_class"], purpose=purpose,
                    operation="store", at=valid_from,
                ):
                    raise ValidationError("ConsentGrant does not authorize this semantic observation")
            event_payload = {
                "ontology_schema_version": value["record_schema_version"],
                "node_id": node_id,
                "node_type": node_type,
                "payload": payload,
                "provenance": provenance,
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, f"semantic_{provenance['status']}", operator_id, now, valid_from,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload), None,
                 provenance["status"]),
            )
            conn.execute("INSERT INTO nodes VALUES(?,?,?,?)", (node_id, node_type, operator_id, event_id))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), node_id, canonical_bytes(payload).decode(), payload_sha256(payload),
                 provenance["status"], provenance["authority_tier"],
                 canonical_bytes(version_provenance(
                     status=provenance["status"], authority_tier=provenance["authority_tier"],
                     actor_class=provenance["actor_class"], actor_id=provenance["actor_id"],
                     mechanism=provenance["mechanism"], event_id=event_id,
                     model=provenance["model"], ratifier=provenance["ratifier_id"],
                 )).decode(), json.dumps(evidence_ids), valid_from, None,
                 now, None, event_id, None),
            )
        return node_id

    def append_semantic_relation(self, contract: dict[str, Any], *, valid_from: str) -> str:
        """Persist one closed, evidence-linked relation between typed nodes."""
        value = validate_relation_contract(contract)
        try:
            parsed_valid_from = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValidationError("valid_from must be an RFC3339 timestamp") from exc
        if parsed_valid_from.tzinfo is None:
            raise ValidationError("valid_from must include timezone")
        operator_id = value["operator_id"]
        self._require_configured_operator(operator_id)
        provenance = value["provenance"]
        evidence_ids = provenance["evidence_ids"]
        relation_id = value["relation_id"]
        event_id = make_urn("event")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            evidence_ids = self._require_evidence(conn, evidence_ids)
            endpoints = conn.execute(
                "SELECT node_id,node_type,operator_id FROM nodes WHERE node_id IN (?,?)",
                (value["source_id"], value["target_id"]),
            ).fetchall()
            by_id = {row["node_id"]: row for row in endpoints}
            if set(by_id) != {value["source_id"], value["target_id"]}:
                raise ValidationError("semantic relation endpoints must exist")
            if by_id[value["source_id"]]["node_type"] != value["source_type"] or by_id[value["target_id"]]["node_type"] != value["target_type"]:
                raise ValidationError("semantic relation endpoint types do not match canonical nodes")
            if {row["operator_id"] for row in endpoints} != {operator_id}:
                raise ValidationError("cross-operator semantic relation is forbidden")
            payload = {
                "ontology_schema_version": value["record_schema_version"],
                "relation": value["relation_type"],
                "evidence_mode": value["evidence_mode"],
                "why": value["why"],
            }
            event_payload = {**value, "provenance": provenance}
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "semantic_relation", operator_id, now, valid_from,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload), None,
                 provenance["status"]),
            )
            conn.execute(
                "INSERT INTO edges VALUES(?,?,?,?,?,?)",
                (relation_id, value["relation_type"], value["source_id"], value["target_id"], operator_id, event_id),
            )
            conn.execute(
                "INSERT INTO edge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("edge-version"), relation_id, canonical_bytes(payload).decode(), payload_sha256(payload),
                 provenance["status"], provenance["authority_tier"],
                 canonical_bytes(version_provenance(
                     status=provenance["status"], authority_tier=provenance["authority_tier"],
                     actor_class=provenance["actor_class"], actor_id=provenance["actor_id"],
                     mechanism=provenance["mechanism"], event_id=event_id,
                     model=provenance["model"], ratifier=provenance["ratifier_id"],
                     relation=value["relation_type"],
                 )).decode(), json.dumps(evidence_ids), valid_from, None,
                 now, None, event_id, None),
            )
        return relation_id

    def revoke_consent(
        self, grant_id: str, *, operator_id: str, reason: str,
        revoked_at: str | None = None,
    ) -> str:
        """Append a durable ConsentGrant revocation; never rewrite the grant."""
        require_urn(operator_id, "operator")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("consent revocation reason is required")
        revoked_at = revoked_at or utc_now()
        try:
            parsed = datetime.fromisoformat(revoked_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValidationError("revoked_at must be an RFC3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValidationError("revoked_at must include timezone")
        event_id = make_urn("event")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,n.node_type,nv.* FROM nodes n JOIN node_versions nv USING(node_id)
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (grant_id,)).fetchone()
            if not current or current["node_type"] != "ConsentGrant":
                raise ValidationError("current ConsentGrant does not exist")
            if current["operator_id"] != operator_id:
                raise ValidationError("only the grant operator may revoke consent")
            payload = json.loads(current["payload_json"])
            if payload.get("revoked_at") is not None:
                raise ConflictError("ConsentGrant is already revoked")
            payload["revoked_at"] = revoked_at
            payload["revocation_reason"] = reason.strip()
            from imprint.ontology.operator import validate_operator_payload
            payload = validate_operator_payload("ConsentGrant", payload)
            event_payload = {
                "grant_id": grant_id, "revoked_at": revoked_at,
                "revoked_by": operator_id, "reason": reason.strip(),
                "prior_version_id": current["version_id"],
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "consent_revoked", operator_id, now, revoked_at,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload),
                 current["event_id"], "captured"),
            )
            conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), grant_id, canonical_bytes(payload).decode(), payload_sha256(payload),
                 "captured", "captured_judgment", canonical_bytes(version_provenance(
                     status="captured", authority_tier="captured_judgment", actor_class="operator",
                     actor_id=operator_id, mechanism="explicit_consent_revocation", event_id=event_id,
                 )).decode(), current["evidence_json"], current["valid_from"], revoked_at,
                 now, None, event_id, current["version_id"]),
            )
        return event_id

    def append_proposal(self, proposal: dict[str, Any]) -> str:
        """Materialize one validated proposal as a reviewable, non-authoritative node.

        Proposal identity is supplied by the closed proposal envelope. Replaying the
        same bytes is a no-op; reusing an identity for different bytes is a conflict.
        All referenced captured facts are checked inside the writer transaction.
        """
        from imprint.derive.proposals import validate_proposal

        value = validate_proposal(proposal)
        proposal_id = value["proposal_id"]
        source_event_id = value["source_input_event_id"]
        references = value["references"]
        evidence_ids = list(dict.fromkeys(references["evidence_ids"]))
        content_hash = payload_sha256(value)
        now = utc_now()
        event_id = make_urn("event")
        provenance = value["provenance"]
        status = provenance["status"]
        tier = provenance["authority_tier"]
        allowed_tiers = {
            "inferred": {"inferred_candidate", "observed_candidate"},
            "extracted": {"inferred_candidate", "observed_candidate"},
        }
        if tier not in allowed_tiers[status]:
            raise ValidationError("proposal authority tier is incompatible with provenance status")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                """SELECT nv.payload_sha256 FROM nodes n JOIN node_versions nv USING(node_id)
                   WHERE n.node_id=? AND n.node_type='Proposal' ORDER BY nv.system_from LIMIT 1""",
                (proposal_id,),
            ).fetchone()
            if prior:
                if prior[0] == content_hash:
                    return "duplicate"
                raise ConflictError("same proposal_id has different bytes")
            source = conn.execute(
                "SELECT operator_id,valid_time,event_type,payload_json FROM events WHERE event_id=?",
                (source_event_id,),
            ).fetchone()
            if not source or source["event_type"] != "captured":
                raise ValidationError("proposal source_input_event_id is not a captured canonical event")
            source_payload = json.loads(source["payload_json"])
            if self.expected_operator_id is not None and source["operator_id"] != self.expected_operator_id:
                raise ValidationError("proposal source does not match the configured canonical operator")
            if self.expected_node_id is not None and source_payload.get("node_id") != self.expected_node_id:
                raise ValidationError("proposal source does not match the configured producer node")
            for kind, ref_id in (("Case", references["case_id"]), ("Verdict", references["verdict_id"])):
                known = conn.execute(
                    "SELECT 1 FROM nodes WHERE node_id=? AND node_type=? AND created_event_id=?",
                    (ref_id, kind, source_event_id),
                ).fetchone()
                if not known:
                    raise ValidationError(f"proposal {kind.lower()} reference does not belong to its source event")
            for evidence_id in evidence_ids:
                known = conn.execute(
                    """SELECT 1 FROM nodes n JOIN source_receipts sr ON sr.source_id=n.node_id
                       WHERE n.node_id=? AND n.node_type='Evidence'
                         AND n.created_event_id=? AND sr.event_id=?""",
                    (evidence_id, source_event_id, source_event_id),
                ).fetchone()
                if not known:
                    raise ValidationError("proposal evidence reference does not belong to its source event")
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "proposal_submitted", source["operator_id"], now, source["valid_time"],
                 canonical_bytes(value).decode(), content_hash, source_event_id, status),
            )
            conn.execute(
                "INSERT INTO nodes VALUES(?,?,?,?)",
                (proposal_id, "Proposal", source["operator_id"], event_id),
            )
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), proposal_id, canonical_bytes(value).decode(), content_hash,
                 status, tier, canonical_bytes(version_provenance(
                     status=status, authority_tier=tier,
                     actor_class="model" if provenance["model"] else "software",
                     actor_id=provenance["proposer"], mechanism="validated_proposal_spool",
                     event_id=event_id, model=provenance["model"],
                     prompt_recipe=provenance["prompt_recipe_hash"], proposal_id=proposal_id,
                 )).decode(), json.dumps(evidence_ids), source["valid_time"], None,
                 now, None, event_id, None),
            )
        return "applied"

    def ratify_node(self, node_id: str, *, ratifier: str, note: str = "") -> str:
        """Promote an inferred/extracted object through an explicit append-only event."""
        if not isinstance(ratifier, str) or not ratifier.strip():
            raise ValidationError("ratifier identity is required")
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,n.node_type,nv.*,ce.event_type AS created_event_type
              FROM nodes n JOIN node_versions nv USING(node_id)
              JOIN events ce ON ce.event_id=n.created_event_id
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (node_id,)).fetchone()
            if not current:
                raise ValidationError("node is missing or not current")
            if current["node_type"] in {"Proposal", "FeedbackProfile"}:
                raise ValidationError(
                    f"{current['node_type']} records cannot be ratified as ontology authority"
                )
            if current["provenance_status"] not in {"inferred", "extracted"}:
                raise ValidationError("only inferred or extracted objects may be ratified")
            if current["created_event_type"].startswith("semantic_"):
                require_urn(ratifier, "operator")
                self._require_configured_operator(current["operator_id"])
                if ratifier != current["operator_id"]:
                    raise ValidationError("typed semantic authority may be ratified only by its operator")
            next_payload_json = current["payload_json"]
            next_payload_sha256 = current["payload_sha256"]
            if current["node_type"] in {"SelfModelAssertion", "InterventionRule"}:
                from imprint.ontology.operator import validate_operator_payload
                next_payload = json.loads(current["payload_json"])
                next_payload["review_state"] = "confirmed"
                next_payload["provenance"] = {
                    **next_payload["provenance"],
                    "status": "ratified", "actor_class": "operator", "actor_id": ratifier,
                }
                next_payload = validate_operator_payload(current["node_type"], next_payload)
                next_payload_json = canonical_bytes(next_payload).decode()
                next_payload_sha256 = payload_sha256(next_payload)
            event_payload = {
                "node_id": node_id,
                "prior_version_id": current["version_id"],
                "prior_status": current["provenance_status"],
                "new_status": "ratified",
                "ratified_by": ratifier,
                "ratification_note": note,
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "ratified", current["operator_id"], now, now,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload), current["event_id"],
                 "ratified"),
            )
            conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), node_id, next_payload_json, next_payload_sha256,
                 "ratified", "ratified_knowledge", canonical_bytes(version_provenance(
                     status="ratified", authority_tier="ratified_knowledge", actor_class="operator",
                     actor_id=ratifier, mechanism="explicit_ratification", event_id=event_id,
                     proposal_id=current["event_id"], ratifier=ratifier,
                 )).decode(), current["evidence_json"], current["valid_from"],
                 current["valid_to"], now, None, event_id, current["version_id"]),
            )
        return event_id

    def correct_typed_node(
        self, node_id: str, *, corrected_contract: dict[str, Any],
        corrector: str, reason: str,
    ) -> str:
        """Replace a typed inference with an operator-authored, validated correction.

        The inferred proposal remains in ``node_versions`` as a closed version.  The
        new head is linked to it both by ``prior_version_id`` and by the correction
        event, so correction never rewrites or conceals what the model proposed.
        """
        require_urn(corrector, "operator")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("correction reason is required")
        value = validate_node_contract(corrected_contract)
        if value["node_id"] != node_id:
            raise ValidationError("corrected contract must retain the original node_id")
        if value["node_type"] not in {"SelfModelAssertion", "InterventionRule"}:
            raise ValidationError("only SelfModelAssertion or InterventionRule may use typed correction")
        provenance = value["provenance"]
        if provenance["status"] != "ratified" or provenance["actor_class"] != "operator":
            raise ValidationError("typed correction must be operator-authored ratified knowledge")
        if provenance["actor_id"] != corrector or provenance["ratifier_id"] != corrector:
            raise ValidationError("typed correction actor and ratifier must be the correcting operator")
        if value["payload"]["review_state"] != "corrected":
            raise ValidationError("typed correction payload review_state must be corrected")

        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,n.node_type,nv.*,ce.event_type AS created_event_type
              FROM nodes n JOIN node_versions nv USING(node_id)
              JOIN events ce ON ce.event_id=n.created_event_id
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (node_id,)).fetchone()
            if not current:
                raise ValidationError("node is missing or not current")
            if current["node_type"] not in {"SelfModelAssertion", "InterventionRule"}:
                raise ValidationError("only SelfModelAssertion or InterventionRule may use typed correction")
            if not current["created_event_type"].startswith("semantic_"):
                raise ValidationError("typed correction requires a semantic proposal")
            if current["provenance_status"] not in {"inferred", "extracted"}:
                raise ValidationError("only inferred or extracted typed proposals may be corrected")
            if value["node_type"] != current["node_type"]:
                raise ValidationError("corrected contract must retain the original node_type")
            if current["operator_id"] != corrector or value["operator_id"] != corrector:
                raise ValidationError("typed proposal may be corrected only by its operator")
            self._require_configured_operator(current["operator_id"])
            evidence_ids = self._require_evidence(conn, provenance["evidence_ids"])

            def node_lookup(identifier: str) -> tuple[str, str] | None:
                row = conn.execute(
                    "SELECT node_type,operator_id FROM nodes WHERE node_id=?",
                    (identifier,),
                ).fetchone()
                if row:
                    return row["node_type"], row["operator_id"]
                receipt = conn.execute(
                    """SELECT e.operator_id FROM source_receipts sr
                       JOIN events e USING(event_id) WHERE sr.source_id=?""",
                    (identifier,),
                ).fetchone()
                return ("Evidence", receipt["operator_id"]) if receipt else None

            def version_lookup(identifier: str) -> tuple[str, str] | None:
                row = conn.execute(
                    """SELECT nv.node_id,n.operator_id FROM node_versions nv
                       JOIN nodes n USING(node_id) WHERE nv.version_id=?""",
                    (identifier,),
                ).fetchone()
                return (row["node_id"], row["operator_id"]) if row else None

            validate_payload_references(
                value["node_type"], value["payload"], operator_id=corrector,
                provenance_evidence_ids=evidence_ids,
                node_lookup=node_lookup, version_lookup=version_lookup,
            )
            event_payload = {
                "node_id": node_id,
                "node_type": current["node_type"],
                "prior_version_id": current["version_id"],
                "prior_event_id": current["event_id"],
                "prior_status": current["provenance_status"],
                "corrected_by": corrector,
                "reason": reason.strip(),
                "replacement_payload_sha256": payload_sha256(value["payload"]),
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "corrected", current["operator_id"], now, now,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload),
                 current["event_id"], "ratified"),
            )
            conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), node_id, canonical_bytes(value["payload"]).decode(),
                 payload_sha256(value["payload"]), "ratified", "ratified_knowledge",
                 canonical_bytes(version_provenance(
                     status="ratified", authority_tier="ratified_knowledge", actor_class="operator",
                     actor_id=corrector, mechanism=provenance["mechanism"], event_id=event_id,
                     model=None, proposal_id=current["event_id"], ratifier=corrector,
                 )).decode(), json.dumps(evidence_ids), current["valid_from"],
                 current["valid_to"], now, None, event_id, current["version_id"]),
            )
        return event_id

    def contest_typed_node(self, node_id: str, *, contestor: str, reason: str) -> str:
        """Record an operator's explicit contest and close the typed proposal head."""
        require_urn(contestor, "operator")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("contest reason is required")
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,n.node_type,nv.*,ce.event_type AS created_event_type
              FROM nodes n JOIN node_versions nv USING(node_id)
              JOIN events ce ON ce.event_id=n.created_event_id
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (node_id,)).fetchone()
            if not current:
                raise ValidationError("node is missing or not current")
            if current["node_type"] not in {"SelfModelAssertion", "InterventionRule"}:
                raise ValidationError("only SelfModelAssertion or InterventionRule may be contested")
            if not current["created_event_type"].startswith("semantic_"):
                raise ValidationError("typed contest requires a semantic proposal")
            if current["provenance_status"] not in {"inferred", "extracted"}:
                raise ValidationError("only inferred or extracted typed proposals may be contested")
            if current["operator_id"] != contestor:
                raise ValidationError("typed proposal may be contested only by its operator")
            self._require_configured_operator(current["operator_id"])
            event_payload = {
                "node_id": node_id, "node_type": current["node_type"],
                "prior_version_id": current["version_id"],
                "prior_event_id": current["event_id"],
                "prior_status": current["provenance_status"],
                "contested_by": contestor, "reason": reason.strip(),
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "contested", current["operator_id"], now, now,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload),
                 current["event_id"], "ratified"),
            )
            conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
        return event_id

    def _review_semantic_edge(
        self, edge_id: str, *, action: str, actor: str, reason: str,
        revisit_after: str | None = None,
    ) -> str:
        """Apply an operator-only disposition to an inferred/extracted semantic edge."""
        require_urn(actor, "operator")
        if action not in {"ratified", "deferred", "rejected"}:
            raise ValidationError("unsupported semantic edge review action")
        if not isinstance(reason, str):
            raise ValidationError("edge review reason must be a string")
        if action in {"deferred", "rejected"} and not reason.strip():
            label = {"deferred": "deferral", "rejected": "rejection"}[action]
            raise ValidationError(f"edge {label} reason is required")
        if revisit_after is not None:
            try:
                parsed = datetime.fromisoformat(revisit_after.replace("Z", "+00:00"))
            except (AttributeError, ValueError) as exc:
                raise ValidationError("revisit_after must be an RFC3339 timestamp") from exc
            if parsed.tzinfo is None:
                raise ValidationError("revisit_after must include timezone")
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT e.operator_id,e.edge_type,e.source_id,e.target_id,e.created_event_id,
                     sn.node_type AS source_type,tn.node_type AS target_type,
                     ev.*,ce.event_type AS created_event_type
              FROM edges e JOIN edge_versions ev USING(edge_id)
              JOIN nodes sn ON sn.node_id=e.source_id
              JOIN nodes tn ON tn.node_id=e.target_id
              JOIN events ce ON ce.event_id=e.created_event_id
              WHERE e.edge_id=? AND ev.system_to IS NULL
            """, (edge_id,)).fetchone()
            if not current:
                raise ValidationError("edge is missing or not current")
            if current["created_event_type"] != "semantic_relation":
                raise ValidationError("only typed semantic relations may use edge review")
            if current["provenance_status"] not in {"inferred", "extracted"}:
                raise ValidationError("only inferred or extracted semantic relations may be reviewed")
            if current["operator_id"] != actor:
                raise ValidationError("semantic relation may be reviewed only by its operator")
            self._require_configured_operator(current["operator_id"])
            event_payload = {
                "edge_id": edge_id, "edge_type": current["edge_type"],
                "prior_version_id": current["version_id"],
                "prior_event_id": current["event_id"],
                "prior_status": current["provenance_status"],
                "reviewed_by": actor, "disposition": action,
                "reason": reason.strip(), "revisit_after": revisit_after,
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, f"edge_{action}", current["operator_id"], now, now,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload),
                 current["event_id"], "ratified" if action in {"ratified", "rejected"} else current["provenance_status"]),
            )
            if action == "deferred":
                return event_id
            conn.execute("UPDATE edge_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
            if action == "ratified":
                next_payload = json.loads(current["payload_json"])
                ratified_provenance = {
                    "status": "ratified", "authority_tier": "ratified_knowledge",
                    "actor_class": "operator", "actor_id": actor,
                    "mechanism": "explicit_edge_ratification",
                    "evidence_ids": json.loads(current["evidence_json"]),
                    "model": None, "ratifier_id": actor,
                }
                validate_relation_contract({
                    "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
                    "relation_id": edge_id, "relation_type": current["edge_type"],
                    "source_id": current["source_id"], "source_type": current["source_type"],
                    "target_id": current["target_id"], "target_type": current["target_type"],
                    "operator_id": current["operator_id"],
                    "evidence_mode": next_payload["evidence_mode"],
                    "why": next_payload["why"], "provenance": ratified_provenance,
                })
                conn.execute(
                    "INSERT INTO edge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (make_urn("edge-version"), edge_id, canonical_bytes(next_payload).decode(),
                     payload_sha256(next_payload),
                     "ratified", "ratified_knowledge", canonical_bytes(version_provenance(
                         status="ratified", authority_tier="ratified_knowledge", actor_class="operator",
                         actor_id=actor, mechanism="explicit_edge_ratification", event_id=event_id,
                         proposal_id=current["event_id"], ratifier=actor,
                         relation=current["edge_type"],
                     )).decode(), current["evidence_json"], current["valid_from"], current["valid_to"],
                     now, None, event_id, current["version_id"]),
                )
        return event_id

    def ratify_edge(self, edge_id: str, *, ratifier: str, note: str = "") -> str:
        return self._review_semantic_edge(edge_id, action="ratified", actor=ratifier, reason=note)

    def defer_edge(
        self, edge_id: str, *, reviewer: str, reason: str,
        revisit_after: str | None = None,
    ) -> str:
        return self._review_semantic_edge(
            edge_id, action="deferred", actor=reviewer, reason=reason,
            revisit_after=revisit_after,
        )

    def reject_edge(self, edge_id: str, *, rejector: str, reason: str) -> str:
        return self._review_semantic_edge(edge_id, action="rejected", actor=rejector, reason=reason)

    def reject_node(self, node_id: str, *, rejector: str, reason: str) -> str:
        """Close a proposal without erasing its inspectable history."""
        if not rejector.strip() or not reason.strip():
            raise ValidationError("rejector and rejection reason are required")
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,nv.*,ce.event_type AS created_event_type
              FROM nodes n JOIN node_versions nv USING(node_id)
              JOIN events ce ON ce.event_id=n.created_event_id
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (node_id,)).fetchone()
            if not current:
                raise ValidationError("node is missing or not current")
            if current["provenance_status"] not in {"inferred", "extracted"}:
                raise ValidationError("only inferred or extracted objects may be rejected")
            if current["created_event_type"].startswith("semantic_"):
                require_urn(rejector, "operator")
                self._require_configured_operator(current["operator_id"])
                if rejector != current["operator_id"]:
                    raise ValidationError("typed semantic proposal may be rejected only by its operator")
            payload = {
                "node_id": node_id,
                "prior_version_id": current["version_id"],
                "prior_status": current["provenance_status"],
                "rejected_by": rejector,
                "reason": reason,
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "rejected", current["operator_id"], now, now,
                 canonical_bytes(payload).decode(), payload_sha256(payload), current["event_id"],
                 "ratified"),
            )
            conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
        return event_id

    def defer_node(
        self,
        node_id: str,
        *,
        reviewer: str,
        reason: str,
        revisit_after: str | None = None,
    ) -> str:
        """Record an explicit no-decision without changing proposal authority.

        Deferral is an inspectable review event, not an implicit absence of a
        ratification.  The inferred/extracted head stays current and remains
        ineligible for authoritative retrieval.
        """
        reviewer = self._require_actor(reviewer)
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("deferral reason is required")
        if revisit_after is not None:
            if not isinstance(revisit_after, str):
                raise ValidationError("revisit_after must be an RFC3339 timestamp")
            try:
                parsed_revisit = datetime.fromisoformat(revisit_after.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError("revisit_after must be an RFC3339 timestamp") from exc
            if parsed_revisit.tzinfo is None:
                raise ValidationError("revisit_after must include timezone")
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,n.node_type,nv.*,ce.event_type AS created_event_type
              FROM nodes n JOIN node_versions nv USING(node_id)
              JOIN events ce ON ce.event_id=n.created_event_id
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (node_id,)).fetchone()
            if not current:
                raise ValidationError("node is missing or not current")
            if current["provenance_status"] not in {"inferred", "extracted"}:
                raise ValidationError("only inferred or extracted objects may be deferred")
            if current["created_event_type"].startswith("semantic_"):
                require_urn(reviewer, "operator")
                self._require_configured_operator(current["operator_id"])
                if reviewer != current["operator_id"]:
                    raise ValidationError("typed semantic proposal may be deferred only by its operator")
            payload = {
                "node_id": node_id,
                "node_type": current["node_type"],
                "version_id": current["version_id"],
                "reviewed_by": reviewer,
                "reason": reason.strip(),
                "revisit_after": revisit_after,
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "deferred", current["operator_id"], now, now,
                 canonical_bytes(payload).decode(), payload_sha256(payload), current["event_id"],
                 current["provenance_status"]),
            )
            if current["node_type"] in {"SelfModelAssertion", "InterventionRule"}:
                from imprint.ontology.operator import validate_operator_payload
                next_payload = json.loads(current["payload_json"])
                next_payload["review_state"] = "deferred"
                next_payload = validate_operator_payload(current["node_type"], next_payload)
                next_provenance = json.loads(current["provenance_json"])
                next_provenance["event_id"] = event_id
                conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
                conn.execute(
                    "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (make_urn("node-version"), node_id, canonical_bytes(next_payload).decode(),
                     payload_sha256(next_payload), current["provenance_status"], current["authority_tier"],
                     canonical_bytes(next_provenance).decode(), current["evidence_json"],
                     current["valid_from"], current["valid_to"], now, None, event_id, current["version_id"]),
                )
        return event_id

    def node_history(self, node_id: str) -> dict[str, Any]:
        """Return every immutable version, including a closed rejected/tombstoned head."""
        with self.connect() as conn:
            rows = conn.execute("""
              SELECT nv.*,n.node_type,n.operator_id,e.event_type,e.payload_json AS event_payload_json
              FROM nodes n JOIN node_versions nv USING(node_id)
              JOIN events e ON e.event_id=nv.event_id
              WHERE n.node_id=? ORDER BY nv.system_from,nv.version_id
            """, (node_id,)).fetchall()
            dispositions = conn.execute("""
              SELECT event_id,event_type,system_time,payload_json FROM events
              WHERE event_type IN (
                'ratified','rejected','deferred','corrected','contested',
                'consent_revoked','tombstoned','reason_added','reinforced',
                'contradicts','supersedes','domain_selected','domain_frozen'
              )
                AND (payload_json LIKE ? OR payload_json LIKE ? OR payload_json LIKE ?)
              ORDER BY system_time,event_id
            """, (
                f'%"node_id":"{node_id}"%', f'%"source_id":"{node_id}"%',
                f'%"target_id":"{node_id}"%',
            )).fetchall()
        if not rows:
            raise ValidationError("node does not exist")
        versions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["event_payload"] = json.loads(item.pop("event_payload_json"))
            item["provenance"] = json.loads(item.pop("provenance_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            versions.append(item)
        return {
            "node_id": node_id,
            "versions": versions,
            "dispositions": [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "system_time": row["system_time"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in dispositions
            ],
        }

    def add_reason(
        self,
        verdict_id: str,
        *,
        reason: str,
        actor_id: str,
        source_locator: str = "explicit_cli",
    ) -> str:
        """Append a later WHY and evidence; never rewrite the original null payload."""
        if not reason.strip() or not actor_id.strip():
            raise ValidationError("reason and actor_id are required")
        return self._append_verdict_evidence(
            verdict_id,
            content=reason,
            actor_id=actor_id,
            source_locator=source_locator,
            event_type="reason_added",
            payload_update={"reason": reason, "reason_status": "later_added"},
        )

    def reinforce_verdict(
        self,
        verdict_id: str,
        *,
        evidence_text: str,
        actor_id: str,
        source_locator: str = "explicit_cli",
    ) -> str:
        """Append supporting evidence and a new Verdict version without changing the call."""
        if not evidence_text.strip() or not actor_id.strip():
            raise ValidationError("evidence_text and actor_id are required")
        return self._append_verdict_evidence(
            verdict_id,
            content=evidence_text,
            actor_id=actor_id,
            source_locator=source_locator,
            event_type="reinforced",
            payload_update={},
        )

    def _append_verdict_evidence(
        self,
        verdict_id: str,
        *,
        content: str,
        actor_id: str,
        source_locator: str,
        event_type: str,
        payload_update: dict[str, Any],
    ) -> str:
        now = utc_now()
        event_id = make_urn("event")
        evidence_id = make_urn("evidence")
        evidence_payload = {
            "evidence_id": evidence_id,
            "kind": "operator_verbatim",
            "content": content,
            "content_sha256": payload_sha256(content),
            "source_locator": source_locator,
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,n.node_type,nv.* FROM nodes n JOIN node_versions nv USING(node_id)
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (verdict_id,)).fetchone()
            if not current or current["node_type"] != "Verdict":
                raise ValidationError("current Verdict does not exist")
            prior_payload = json.loads(current["payload_json"])
            if event_type == "reason_added" and prior_payload.get("reason") is not None:
                raise ValidationError("Verdict already has a reason; use a later call to supersede it")
            new_payload = {**prior_payload, **payload_update}
            prior_evidence = json.loads(current["evidence_json"])
            new_evidence = list(dict.fromkeys([*prior_evidence, evidence_id]))
            event_payload = {
                "node_id": verdict_id,
                "prior_version_id": current["version_id"],
                "evidence_id": evidence_id,
                "actor_id": actor_id,
                "source_locator": source_locator,
            }
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, event_type, current["operator_id"], now, now,
                 canonical_bytes(event_payload).decode(), payload_sha256(event_payload), current["event_id"],
                 "captured"),
            )
            conn.execute("INSERT INTO nodes VALUES(?,?,?,?)", (evidence_id, "Evidence", current["operator_id"], event_id))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), evidence_id, canonical_bytes(evidence_payload).decode(),
                 payload_sha256(evidence_payload), "captured", "captured_judgment", canonical_bytes(version_provenance(
                     status="captured", authority_tier="captured_judgment", actor_class="operator",
                     actor_id=actor_id, mechanism=source_locator, event_id=event_id,
                 )).decode(), json.dumps([evidence_id]),
                 now, None, now, None, event_id, None),
            )
            conn.execute(
                "INSERT INTO source_receipts VALUES(?,?,?,?,?)",
                (evidence_id, "operator_verbatim", source_locator, evidence_payload["content_sha256"], event_id),
            )
            self._insert_edge_for_event(
                conn, "supported_by", verdict_id, evidence_id, current["operator_id"], event_id, now,
                evidence_id,
            )
            conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
            conn.execute(
                "INSERT INTO node_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (make_urn("node-version"), verdict_id, canonical_bytes(new_payload).decode(), payload_sha256(new_payload),
                 "captured", "captured_judgment", canonical_bytes(version_provenance(
                     status="captured", authority_tier="captured_judgment", actor_class="operator",
                     actor_id=actor_id, mechanism=source_locator, event_id=event_id,
                 )).decode(), json.dumps(new_evidence), current["valid_from"],
                 current["valid_to"], now, None, event_id, current["version_id"]),
            )
        return event_id

    def _insert_edge_for_event(
        self, conn, edge_type: str, source_id: str, target_id: str, operator_id: str,
        event_id: str, now: str, evidence_id: str,
    ) -> None:
        edge_id = make_urn("edge")
        payload = {"why": "explicit later operator evidence", "relation": edge_type}
        conn.execute("INSERT INTO edges VALUES(?,?,?,?,?,?)", (edge_id, edge_type, source_id, target_id, operator_id, event_id))
        conn.execute(
            "INSERT INTO edge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (make_urn("edge-version"), edge_id, canonical_bytes(payload).decode(), payload_sha256(payload),
             "captured", "captured_judgment", canonical_bytes(version_provenance(
                 status="captured", authority_tier="captured_judgment", actor_class="operator",
                 actor_id=operator_id, mechanism="explicit_later_evidence", event_id=event_id,
                 relation=edge_type,
             )).decode(), json.dumps([evidence_id]), now, None, now, None, event_id, None),
        )

    def tombstone_node(self, node_id: str, *, reason: str) -> str:
        if not reason.strip():
            raise ValidationError("tombstone reason is required")
        now = utc_now()
        event_id = make_urn("event")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
              SELECT n.operator_id,nv.* FROM nodes n JOIN node_versions nv USING(node_id)
              WHERE n.node_id=? AND nv.system_to IS NULL
            """, (node_id,)).fetchone()
            if not current:
                raise ValidationError("node is missing or already tombstoned")
            payload = {"node_id": node_id, "reason": reason, "prior_version_id": current["version_id"]}
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, "tombstoned", current["operator_id"], now, now,
                 canonical_bytes(payload).decode(), payload_sha256(payload), current["event_id"],
                 current["provenance_status"]),
            )
            conn.execute("UPDATE node_versions SET system_to=? WHERE version_id=?", (now, current["version_id"]))
            conn.execute("UPDATE edge_versions SET system_to=? WHERE edge_id IN (SELECT edge_id FROM edges WHERE source_id=? OR target_id=?) AND system_to IS NULL", (now, node_id, node_id))
        return event_id

    def current_edges(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
              SELECT e.edge_id,e.edge_type,e.source_id,e.target_id,ev.payload_json,
                     ev.payload_sha256,ev.provenance_status,ev.authority_tier,ev.provenance_json,ev.evidence_json,
                     ev.valid_from,ev.valid_to,ev.system_from,ev.system_to,ev.event_id
              FROM edges e JOIN edge_versions ev USING(edge_id)
              WHERE ev.system_to IS NULL ORDER BY e.edge_type,e.edge_id
            """).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["provenance"] = json.loads(item.pop("provenance_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "store_schema_version": STORE_SCHEMA_VERSION,
            "ontology_schema_version": ONTOLOGY_SCHEMA_VERSION,
            "nodes": self.current_nodes(),
            "edges": self.current_edges(),
        }
