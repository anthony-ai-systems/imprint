"""The scope carrier: one ``record_scope`` row per record, keyed by node.

The key is the node, so a new version keeps its row with no extra work. Nothing
here decides who sees a record, and no delivery route reads a row yet. A store
no build has initialized since the table landed has no table, which reads as
no rows, because several read paths never call ``initialize``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from imprint.errors import ValidationError

SCOPE_EXTENSION = "org.imprint.scope"
SCOPE_EXTENSION_VERSION = "1.0.0"
SCOPE_KINDS = frozenset({"client", "central", "system", "unresolved", "held"})
DELIVERY_TAGS = frozenset({"general", "specific"})
# A delivery tag goes only on these: they sit in the ``general`` section, and Jev
# can be sent them.
TAGGED_NODE_TYPES = frozenset({"Verdict", "Principle", "Rule"})
RESOLVED_EXPLICIT = "explicit"
SCOPE_COLUMNS = (
    "node_id", "scope_kind", "client_id", "project_id", "delivery", "delivery_rank",
    "speaker", "endorser", "overrides_json", "resolved_by", "event_id", "stamped_at",
)
_STAMP_KEYS = frozenset({
    "scope_kind", "client_id", "project_id", "resolved_by", "candidate_client_ids",
    "speaker", "endorser", "delivery", "delivery_rank",
})
SCOPE_CHANGE_KEYS = frozenset({
    "scope_kind", "client_id", "project_id", "delivery", "delivery_rank", "overrides",
})


def _require_text(value: Any, field: str, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"scope {field} must be a non-empty string")


def _require_shape(kind: Any, client_id: Any, delivery: Any, rank: Any) -> None:
    """The table's CHECKs in Python, so a bad value is refused by name."""
    if kind not in SCOPE_KINDS:
        raise ValidationError("unknown scope kind")
    if (kind == "client") != (client_id is not None):
        raise ValidationError("a client id goes on a client scope and on no other kind")
    if delivery is not None:
        if delivery not in DELIVERY_TAGS:
            raise ValidationError("unknown delivery tag")
        if not (kind in {"client", "central"} or (kind == "system" and delivery == "specific")):
            raise ValidationError("this scope kind cannot carry that delivery tag")
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int)):
        raise ValidationError("delivery rank must be an integer")


def validate_stamp(stamp: Any) -> dict[str, Any]:
    """Fail closed on one stamp. No caller of this may write a central row.

    The list of ids a held record could belong to stays on the event: a held
    row's ``client_id`` must stay null, and the list is history, not scope.
    """
    if not isinstance(stamp, Mapping) or set(stamp) - _STAMP_KEYS:
        raise ValidationError("scope stamp has unknown fields")
    value = {key: stamp.get(key) for key in _STAMP_KEYS}
    if value["scope_kind"] == "central":
        raise ValidationError("a central scope is written only at ratification")
    _require_shape(value["scope_kind"], value["client_id"], value["delivery"], value["delivery_rank"])
    _require_text(value["resolved_by"], "resolved_by", nullable=False)
    for field in ("client_id", "project_id", "speaker", "endorser"):
        _require_text(value[field], field)
    if value["speaker"] is not None and value["endorser"] is None:
        raise ValidationError("a speaker needs an endorser")
    candidates = value["candidate_client_ids"] = value["candidate_client_ids"] or []
    if not isinstance(candidates, list) or candidates != sorted(set(candidates)):
        raise ValidationError("candidate_client_ids must be a sorted list")
    for candidate in candidates:
        _require_text(candidate, "candidate_client_ids", nullable=False)
    if candidates and value["scope_kind"] != "held":
        raise ValidationError("candidate_client_ids go only on a held scope")
    return value


def capture_stamp(extensions: Mapping[str, Any]) -> dict[str, Any] | None:
    """The stamp an envelope carries. No extension means no row, so a record stays legacy."""
    extension = extensions.get(SCOPE_EXTENSION)
    if extension is None:
        return None
    if extension.get("schema_version") != SCOPE_EXTENSION_VERSION:
        raise ValidationError("unsupported scope extension version")
    return validate_stamp(extension.get("payload"))


def require_fits_node(value: Mapping[str, Any], node_type: str, payload: Mapping[str, Any]) -> None:
    """Refuse a scope the record itself rules out."""
    from imprint.retrieve.store_source import _SEMANTIC_TYPES

    if node_type not in _SEMANTIC_TYPES:
        raise ValidationError(f"{node_type} is not retrievable, so it carries no scope")
    if value["delivery"] is not None and node_type not in TAGGED_NODE_TYPES:
        raise ValidationError("a delivery tag goes only on a Verdict, Principle or Rule")
    # Import refuses this pair, so writing it here would strand the export.
    if value["scope_kind"] in {"client", "central"} and isinstance(payload.get("domain_id"), str):
        raise ValidationError("a client or central record carries no domain_id")


def merge_change(prior: Mapping[str, Any] | None, changes: Mapping[str, Any]) -> dict[str, Any]:
    """The row ``imprint scope set`` leaves: only what it names moves."""
    if not changes or set(changes) - SCOPE_CHANGE_KEYS:
        raise ValidationError("scope set must name a kind, client, project, delivery, rank or overrides")
    if changes.get("scope_kind") == "central":
        raise ValidationError("a central scope is written only at ratification")
    if prior is not None and prior["scope_kind"] == "central" and "scope_kind" in changes:
        raise ValidationError("a central record keeps its kind")
    if prior is None and "scope_kind" not in changes:
        raise ValidationError("a record with no scope row needs a kind")
    value: dict[str, Any] = {
        "client_id": None, "project_id": None, "delivery": None, "delivery_rank": None,
        "speaker": None, "endorser": None, "overrides": [],
        **(dict(prior) if prior else {}), **changes,
    }
    if "client_id" not in changes and value["scope_kind"] != "client":
        value["client_id"] = None
    _require_shape(value["scope_kind"], value["client_id"], value["delivery"], value["delivery_rank"])
    for field in ("client_id", "project_id"):
        _require_text(value[field], field)
    overrides = value["overrides"]
    if not isinstance(overrides, list):
        raise ValidationError("overrides must be a list")
    for override in overrides:
        _require_text(override, "overrides", nullable=False)
    value["overrides"] = sorted(set(overrides))
    return value


def validate_row(row: Any) -> None:
    """Fail closed on one row a document carries, ``central`` included."""
    if not isinstance(row, dict) or set(row) != set(SCOPE_COLUMNS):
        raise ValidationError("invalid row in record scope")
    _require_shape(row["scope_kind"], row["client_id"], row["delivery"], row["delivery_rank"])
    for field in ("node_id", "resolved_by", "event_id", "stamped_at"):
        _require_text(row[field], field, nullable=False)
    for field in ("client_id", "project_id", "speaker", "endorser"):
        _require_text(row[field], field)
    try:
        overrides = json.loads(row["overrides_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("record scope overrides are invalid") from exc
    if not isinstance(overrides, list) or not all(isinstance(item, str) for item in overrides):
        raise ValidationError("record scope overrides are invalid")


def scope_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_scope'"
    ).fetchone() is not None


def read_scope_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not scope_table_exists(conn):
        return []
    return [dict(row) for row in conn.execute("SELECT * FROM record_scope ORDER BY node_id")]


def read_scope_row(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    if not scope_table_exists(conn):
        return None
    row = conn.execute("SELECT * FROM record_scope WHERE node_id=?", (node_id,)).fetchone()
    return dict(row) if row else None


def scope_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as a reader sees it, with its overrides decoded."""
    value = dict(row)
    value["overrides"] = json.loads(value.pop("overrides_json"))
    return value


def write_scope_row(conn: sqlite3.Connection, row: Mapping[str, Any], *, replace: bool = False) -> None:
    """Write one whole row. A row already on the node stands unless ``replace`` is set."""
    names, marks = ",".join(SCOPE_COLUMNS), ",".join("?" for _ in SCOPE_COLUMNS)
    head, tail = ("INSERT OR REPLACE", "") if replace else ("INSERT", " ON CONFLICT(node_id) DO NOTHING")
    conn.execute(
        f"{head} INTO record_scope ({names}) VALUES ({marks}){tail}",
        tuple(row[column] for column in SCOPE_COLUMNS),
    )


def stamp_row(node_id: str, stamp: Mapping[str, Any], *, event_id: str, now: str) -> dict[str, Any]:
    """The row one validated stamp makes. Overrides never travel on a stamp."""
    return {
        "node_id": node_id, "scope_kind": stamp["scope_kind"], "client_id": stamp["client_id"],
        "project_id": stamp["project_id"], "delivery": stamp["delivery"],
        "delivery_rank": stamp["delivery_rank"], "speaker": stamp["speaker"],
        "endorser": stamp["endorser"], "overrides_json": "[]",
        "resolved_by": stamp["resolved_by"], "event_id": event_id, "stamped_at": now,
    }
