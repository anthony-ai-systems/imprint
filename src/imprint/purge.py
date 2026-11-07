"""Explicit irreversible purge with dependency closure and content-free receipts."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .constants import STORE_SCHEMA_VERSION
from .errors import SafetyError, ValidationError
from .ontology.schema import make_urn
from .paths import validate_data_root
from .permissions import secure_directory, secure_file, secure_tree
from .projections import jsonld_document, markdown_document
from .store import ImprintStore
from .store.scope import scope_table_exists
from .store.service import utc_now


OWNED_CONTENT_DIRS = ("spool", "runtime", "projections", "exports", "backups", "quarantine")


def _write_private_text(path: Path, content: str) -> None:
    secure_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    os.close(fd)
    try:
        secure_file(temporary)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    secure_file(path)


def _closure(conn: sqlite3.Connection, scope: str) -> tuple[set[str], set[str], set[str], set[str], str]:
    """Resolve an exact node, operator, session, or source scope."""
    nodes: set[str] = set()
    seed_events: set[str] = set()
    ingest_items: set[str] = set()
    source = conn.execute("SELECT event_id FROM source_receipts WHERE source_id=?", (scope,)).fetchone()
    ingest_source = conn.execute("SELECT item_id FROM ingest_items WHERE source_id=?", (scope,)).fetchall()
    if source or ingest_source:
        scope_class = "source"
        if source:
            seed_events = {str(source[0])}
        ingest_items = {str(row[0]) for row in ingest_source}
    elif conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (scope,)).fetchone():
        nodes = {scope}
        scope_class = "node_dependency_closure"
    elif (
        conn.execute("SELECT 1 FROM events WHERE operator_id=?", (scope,)).fetchone()
        or conn.execute("SELECT 1 FROM ingest_items WHERE operator_id=?", (scope,)).fetchone()
    ):
        scope_class = "operator"
        nodes = {str(row[0]) for row in conn.execute("SELECT node_id FROM nodes WHERE operator_id=?", (scope,))}
        seed_events = {str(row[0]) for row in conn.execute("SELECT event_id FROM events WHERE operator_id=?", (scope,))}
        ingest_items = {str(row[0]) for row in conn.execute("SELECT item_id FROM ingest_items WHERE operator_id=?", (scope,))}
    else:
        session_events = conn.execute(
            "SELECT event_id FROM events WHERE json_valid(payload_json) AND json_extract(payload_json,'$.session_id')=?",
            (scope,),
        ).fetchall()
        session_items = conn.execute("SELECT item_id FROM ingest_items WHERE session_id=?", (scope,)).fetchall()
        if not session_events and not session_items:
            raise ValidationError("purge scope must name an existing node, operator, session, or source")
        scope_class = "session"
        seed_events = {str(row[0]) for row in session_events}
        ingest_items = {str(row[0]) for row in session_items}
    if seed_events and not nodes:
        marks = ",".join("?" for _ in seed_events)
        nodes = {
            str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT node_id FROM node_versions WHERE event_id IN ({marks}) UNION SELECT node_id FROM nodes WHERE created_event_id IN ({marks})",
                [*seed_events, *seed_events],
            )
        }
    if not nodes and not seed_events and not ingest_items:
        raise ValidationError("purge scope resolves to no canonical content")
    while True:
        before = (set(nodes), set(ingest_items))
        if ingest_items:
            item_marks = ",".join("?" for _ in ingest_items)
            nodes |= {
                str(row[0]) for row in conn.execute(
                    f"SELECT kept_node_id FROM ingest_items WHERE item_id IN ({item_marks}) AND kept_node_id IS NOT NULL",
                    list(ingest_items),
                )
            }
        if nodes:
            node_marks = ",".join("?" for _ in nodes)
            ingest_items |= {
                str(row[0]) for row in conn.execute(
                    f"SELECT item_id FROM ingest_items WHERE kept_node_id IN ({node_marks}) OR node_id IN ({node_marks})",
                    [*nodes, *nodes],
                )
            }
            rows = conn.execute(
                f"SELECT source_id,target_id FROM edges WHERE source_id IN ({node_marks}) OR target_id IN ({node_marks})",
                [*nodes, *nodes],
            ).fetchall()
            nodes |= {str(value) for row in rows for value in row}
        if before == (nodes, ingest_items):
            break
    placeholders = ",".join("?" for _ in nodes)
    edges = set()
    events = set(seed_events)
    if nodes:
        edges = {
            str(row[0]) for row in conn.execute(
                f"SELECT edge_id FROM edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                [*nodes, *nodes],
            )
        }
        events |= {
            str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT event_id FROM node_versions WHERE node_id IN ({placeholders})",
                list(nodes),
            )
        }
        ingest_items |= {
            str(row[0]) for row in conn.execute(
                f"SELECT item_id FROM ingest_items WHERE kept_node_id IN ({placeholders}) OR node_id IN ({placeholders})",
                [*nodes, *nodes],
            )
        }
    if edges:
        edge_marks = ",".join("?" for _ in edges)
        events |= {
            str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT event_id FROM edge_versions WHERE edge_id IN ({edge_marks})", list(edges)
            )
        }
    # Disposition-only events (reject/tombstone) have no entity version of their own.
    # Include any ledger event whose canonical JSON explicitly names a closure node.
    for node_id in nodes:
        events |= {
            str(row[0]) for row in conn.execute(
                "SELECT event_id FROM events WHERE payload_json LIKE ?", (f'%"{node_id}"%',)
            )
        }
    if ingest_items:
        item_marks = ",".join("?" for _ in ingest_items)
        events |= {
            str(row[0]) for row in conn.execute(
                f"SELECT event_id FROM ingest_rulings WHERE item_id IN ({item_marks})", list(ingest_items)
            )
        }
    return nodes, edges, events, ingest_items, scope_class


def _drop_scope_rows(conn: sqlite3.Connection, nodes: set[str]) -> None:
    """Take purged ids out of the table and every override list, or the byte scan finds them."""
    if not scope_table_exists(conn):
        return
    conn.execute(f"DELETE FROM record_scope WHERE node_id IN ({','.join('?' for _ in nodes)})", list(nodes))
    for row in conn.execute(
        "SELECT node_id,overrides_json FROM record_scope WHERE overrides_json<>'[]'"
    ).fetchall():
        overrides = json.loads(row["overrides_json"])
        kept = [item for item in overrides if item not in nodes]
        if kept != overrides:
            conn.execute(
                "UPDATE record_scope SET overrides_json=? WHERE node_id=?",
                (json.dumps(kept), row["node_id"]),
            )


def preview_purge(store: ImprintStore, root: Path, scope: str) -> dict[str, Any]:
    root = validate_data_root(root)
    with store.connect() as conn:
        nodes, edges, events, ingest_items, scope_class = _closure(conn, scope)
        receipts = conn.execute(
            f"SELECT COUNT(*) FROM source_receipts WHERE event_id IN ({','.join('?' for _ in events)})",
            list(events),
        ).fetchone()[0] if events else 0
    return {
        "purge_schema_version": "1.0.0",
        "scope_class": scope_class,
        "counts": {
            "nodes": len(nodes), "edges": len(edges), "events": len(events),
            "source_receipts": receipts, "ingest_items": len(ingest_items),
        },
        "active_locations": [str(root / name) for name in OWNED_CONTENT_DIRS if (root / name).exists()],
        "external_backups_exports": "not_discoverable; inventory separately before purge",
        "confirmation_required": scope,
    }


def _remove_owned_files_with_markers(root: Path, markers: list[bytes]) -> list[str]:
    deleted: list[str] = []
    for name in OWNED_CONTENT_DIRS:
        directory = root / name
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if any(marker and marker in content for marker in markers):
                path.unlink()
                deleted.append(str(path.relative_to(root)))
                receipt = path.with_suffix(path.suffix + ".receipt.json")
                if receipt.exists():
                    receipt.unlink()
                    deleted.append(str(receipt.relative_to(root)))
    return deleted


def _scan_active_root(root: Path, markers: list[bytes]) -> list[str]:
    remaining: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"imprint.db", "imprint.db-wal", "imprint.db-shm"}:
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if any(marker and marker in content for marker in markers):
            remaining.append(str(path.relative_to(root)))
    return remaining


def hard_purge(
    store: ImprintStore,
    root: Path,
    scope: str,
    *,
    confirmation: str,
    sentinel: str | None = None,
) -> dict[str, Any]:
    """Purge one connected entity closure after exact, separate confirmation."""
    if confirmation != scope:
        raise SafetyError("purge confirmation must exactly name the scope")
    root = validate_data_root(root)
    operation_id = make_urn("purge")
    purged_at = utc_now()
    with store.connect() as conn:
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")
        nodes, edges, events, ingest_items, scope_class = _closure(conn, scope)
        counts = {
            "nodes": len(nodes), "edges": len(edges), "events": len(events),
            "ingest_items": len(ingest_items),
        }
        node_marks = ",".join("?" for _ in nodes)
        if edges:
            edge_marks = ",".join("?" for _ in edges)
            conn.execute(f"DELETE FROM edge_versions WHERE edge_id IN ({edge_marks})", list(edges))
            conn.execute(f"DELETE FROM edges WHERE edge_id IN ({edge_marks})", list(edges))
        item_marks = ",".join("?" for _ in ingest_items)
        if ingest_items:
            counts["ingest_rulings"] = conn.execute(
                f"SELECT COUNT(*) FROM ingest_rulings WHERE item_id IN ({item_marks})", list(ingest_items)
            ).fetchone()[0]
        if events:
            event_marks = ",".join("?" for _ in events)
            counts["source_receipts"] = conn.execute(
                f"SELECT COUNT(*) FROM source_receipts WHERE event_id IN ({event_marks})", list(events)
            ).fetchone()[0]
            conn.execute(f"DELETE FROM source_receipts WHERE event_id IN ({event_marks})", list(events))
            conn.execute(f"DELETE FROM ingest_rulings WHERE event_id IN ({event_marks})", list(events))
        if ingest_items:
            conn.execute(f"DELETE FROM ingest_rulings WHERE item_id IN ({item_marks})", list(ingest_items))
            conn.execute(f"DELETE FROM ingest_items WHERE item_id IN ({item_marks})", list(ingest_items))
        if nodes:
            _drop_scope_rows(conn, nodes)
            conn.execute(f"DELETE FROM node_versions WHERE node_id IN ({node_marks})", list(nodes))
            conn.execute(f"DELETE FROM nodes WHERE node_id IN ({node_marks})", list(nodes))
        if events:
            event_marks = ",".join("?" for _ in events)
            conn.execute(f"DELETE FROM consumed_inputs WHERE input_event_id IN ({event_marks})", list(events))
            conn.execute(f"DELETE FROM events WHERE event_id IN ({event_marks})", list(events))
        conn.execute(
            "INSERT INTO purge_receipts VALUES(?,?,?,?,?)",
            (operation_id, purged_at, STORE_SCHEMA_VERSION, scope_class, json.dumps(counts, sort_keys=True)),
        )
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("VACUUM")
    finally:
        connection.close()
    projection_dir = root / "projections"
    secure_directory(projection_dir)
    snapshot = store.snapshot()
    markdown_path = projection_dir / "imprint.md"
    jsonld_path = projection_dir / "imprint.jsonld"
    _write_private_text(markdown_path, markdown_document(snapshot))
    _write_private_text(
        jsonld_path,
        json.dumps(jsonld_document(snapshot), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    secure_tree(root)
    markers = [scope.encode("utf-8")]
    if sentinel:
        markers.append(sentinel.encode("utf-8"))
    deleted_files = _remove_owned_files_with_markers(root, markers)
    remaining = _scan_active_root(root, markers)
    database_bytes = store.path.read_bytes()
    if any(marker and marker in database_bytes for marker in markers):
        remaining.append(store.path.name)
    if remaining:
        return {
            "status": "purged_with_residue",
            "operation_id": operation_id,
            "scope_class": scope_class,
            "counts": counts,
            "content_files_removed": len(deleted_files),
            "active_root_scan": "residue",
            "residue_locations": remaining,
            "committed": True,
            "external_backups_exports": "not_discoverable; inventory separately",
        }
    return {
        "status": "purged",
        "operation_id": operation_id,
        "scope_class": scope_class,
        "counts": counts,
        "content_files_removed": len(deleted_files),
        "active_root_scan": "clear",
        "external_backups_exports": "not_discoverable; inventory separately",
    }
