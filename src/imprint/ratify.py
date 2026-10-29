"""Apply one ratification card's recorded answers to a typed-knowledge batch packet.

The card store is read, never written, and only answers its program recorded
count. Canon changes only through ``ImprintStore``: every item is appended as an
evidenced inferred candidate, then ratified, rejected, or deferred by the
configured operator. Ratified pins go to the owner's scope-bindings manifest
under a caller-named root, and the caller-named native judgment reader proves
the result. Node identities derive from the packet bytes, so a re-run resumes
an interrupted batch or changes nothing.

Packet (``imprint-ratification-packet/v1``)::

    {"schema": "imprint-ratification-packet/v1", "card": 9001,
     "model": "model that drafted the statements",
     "items": [{"item": 1, "owner": "example-owner", "node_type": "Principle",
                "statement": "...", "evidence_ids": ["urn:imprint:evidence:..."],
                "scope": {"client_id": "...", "project_id": "...", "domain": "..."},
                "limits": ["..."]}]}

Pattern items add ``case_ids`` (two or more Case URNs). Domain items add
``domain_id`` and ``public_label``; their statement becomes the description.

The card must carry the packet's SHA-256 (``review_packet_sha256``, ``what`` or
``objects``). A card that lists ``ratification_items`` must list exactly the
packet's statements, and its per-item answers are the card program's
``item_answers``, each checked against the saved message it was recorded from.
Answer grammar, case-insensitive: ``<card>.<item> yes|no|fix[ <note>]``,
``<card> all yes``, and a bare ``<card> yes|no|fix[ <note>]`` that counts only on
a single-item card. A card-level no or fix on a multi-item card refuses the batch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .constants import ONTOLOGY_SCHEMA_VERSION, PRODUCT_VERSION
from .errors import ConflictError, ImprintError, ValidationError
from .ontology.contracts import require_urn, validate_node_contract
from .ontology.schema import payload_sha256
from .paths import SYNC_MARKERS
from .permissions import secure_directory, secure_file
from .store import ImprintStore
from .store.service import utc_now


PACKET_SCHEMA = "imprint-ratification-packet/v1"
RECEIPT_SCHEMA = "imprint-ratification-receipt/v1"
BINDINGS_SCHEMA = "native-scope-bindings/v1"
BINDINGS_KIND = "scope_mapping"
MANIFEST_KEYS = ("schema", "binding_kind", "owner", "operator_id", "scope", "bindings")
PIN_KEYS = ("node_id", "version_id", "payload_sha256", "operator_id", "ratification_event_id")
SCOPE_KEYS = ("client_id", "project_id", "domain")
CARD_STORE_ENV = "AGENTS_CARD_STORE"
DEFAULT_CARD_STORE = "~/.local/share/thirty-second-card/state/asks.json"
NODE_TYPES = frozenset({"Principle", "Belief", "Value", "Rule", "Domain", "Pattern"})
ITEM_KEYS = frozenset({"item", "owner", "node_type", "statement", "scope", "evidence_ids", "limits"})
TYPE_KEYS = {"Pattern": frozenset({"case_ids"}), "Domain": frozenset({"domain_id", "public_label"})}
ACTIONS = {"yes": "ratify", "no": "reject", "fix": "defer"}
FINAL_STATES = {"yes": "ratified", "no": "rejected", "fix": "deferred"}
MECHANISM = "ratification_packet_proposal"
MAX_ITEMS = 100
# The reader binds at most 50 evidence refs per node; owner wake routes read at most 20 pins per manifest.
MAX_EVIDENCE = 50
MAX_PINS = 20
READER_LIMIT = 100
MAX_SCOPE_BYTES = 256
MAX_PACKET_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CARD_STORE_BYTES = 64 * 1024 * 1024
READER_TIMEOUT_SECONDS = 120
# Owners and Domain ids are plain slugs; scope values are encoded before they become path parts.
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
PATH_SAFE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
# iCloud Drive syncs these by default on macOS; SYNC_MARKERS covers the other clients.
SYNCED_HOME_DIRS = ("Documents", "Desktop", "Library/Mobile Documents")

_CARD = r"^(?:card\s*)?#?(?P<n>\d+)"
_GAP = r"(?:\s*[-:,)]+\s*|\s+)"
_VERB = r"(?P<verb>yes|no|fix)\b(?P<rest>.*)$"
ITEM_ANSWER = re.compile(_CARD + r"\.(?P<item>\d+)" + _GAP + _VERB, re.I)
ALL_YES = re.compile(_CARD + r"\s+all\s+yes\W*$", re.I)
BARE_ANSWER = re.compile(_CARD + _GAP + _VERB, re.I)
WORD = re.compile(r"[^\W_]")


class Refusal(ImprintError):
    """The packet, the recorded answers, or the store state do not permit a write."""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass
class Decision:
    item: int
    verb: str
    note: str
    answers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ItemPlan:
    item: dict[str, Any]
    decision: Decision
    node_id: str
    contract: dict[str, Any]
    review_text: str
    steps: list[str]
    pinned: bool


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_sha256(value: str) -> str:
    return _sha256(value.encode("utf-8"))


def _file_sha256(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ConflictError(f"expected a regular file: {path}")
    return _sha256(path.read_bytes())


def _read_input(path: Path, label: str, limit: int) -> bytes:
    path = Path(path).expanduser()
    if not path.is_file():
        raise Refusal([f"{label} is not a readable file: {path}"])
    if path.stat().st_size > limit:
        raise Refusal([f"{label} exceeds {limit} bytes: {path}"])
    return path.read_bytes()


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _urn(value: Any, kind: str) -> bool:
    try:
        require_urn(value, kind, kind)
    except ValidationError:
        return False
    return True


def _key(text: str) -> str:
    """Compare notes and reasons the way the card program compares answers."""
    return " ".join(text.casefold().split()).rstrip(".!")


def _unquote(proof: Any) -> str | None:
    """The card program keeps his typed text inside one pair of double quotes."""
    if isinstance(proof, str) and len(proof) >= 2 and proof[0] == proof[-1] == '"':
        return proof[1:-1]
    return proof if isinstance(proof, str) else None


# Packet -------------------------------------------------------------------


def load_packet(path: Path) -> tuple[dict[str, Any], str]:
    raw = _read_input(path, "packet", MAX_PACKET_BYTES)
    try:
        packet = json.loads(raw.decode("utf-8"))
        json.dumps(packet, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise Refusal([f"packet is not UTF-8 JSON ({type(exc).__name__})"]) from exc
    problems = packet_problems(packet)
    if problems:
        raise Refusal(problems)
    return packet, _sha256(raw)


def packet_problems(packet: Any) -> list[str]:
    if not isinstance(packet, dict):
        return ["packet must be a JSON object"]
    expected = {"schema", "card", "model", "items"}
    if set(packet) != expected:
        return [f"packet keys must be exactly {', '.join(sorted(expected))}"]
    problems = []
    if packet["schema"] != PACKET_SCHEMA:
        problems.append(f"packet schema must be {PACKET_SCHEMA}")
    if not _number(packet["card"]):
        problems.append("packet card must be a positive card number")
    if not _text(packet["model"]):
        problems.append("packet model must name the model that drafted the statements")
    items = packet["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        problems.append(f"packet items must be a list of 1 to {MAX_ITEMS} items")
        return problems
    for index, item in enumerate(items, 1):
        problems.extend(f"item {index}: {problem}" for problem in _item_problems(item, index))
    return problems


def _scope_value_ok(value: Any) -> bool:
    """The reader's own rule for one scope value."""
    return (isinstance(value, str) and bool(value) and value == value.strip() and "\x00" not in value
            and len(value.encode("utf-8")) <= MAX_SCOPE_BYTES)


def _item_problems(item: Any, index: int) -> list[str]:
    if not isinstance(item, dict):
        return ["must be an object"]
    node_type = item.get("node_type")
    if node_type not in NODE_TYPES:
        return [f"node_type must be one of {', '.join(sorted(NODE_TYPES))}"]
    expected = ITEM_KEYS | TYPE_KEYS.get(node_type, frozenset())
    missing, unknown = sorted(expected - set(item)), sorted(set(item) - expected)
    if missing or unknown:
        return ([f"missing {', '.join(missing)}"] if missing else []) + (
            [f"unknown {', '.join(unknown)}"] if unknown else [])
    problems = []
    if item["item"] != index or not _number(item["item"]):
        problems.append(f"item number must be {index}; items are numbered 1..N in order")
    if not isinstance(item["owner"], str) or not SAFE_ID.fullmatch(item["owner"]):
        problems.append("owner must be a lowercase identifier")
    if not _text(item["statement"]):
        problems.append("statement must be non-empty text")
    scope = item["scope"]
    if not isinstance(scope, dict) or set(scope) != set(SCOPE_KEYS):
        problems.append("scope must have exactly client_id, project_id and domain")
    else:
        problems.extend(f"scope.{key} must be non-empty text without surrounding spaces, up to "
                        f"{MAX_SCOPE_BYTES} bytes" for key in SCOPE_KEYS if not _scope_value_ok(scope[key]))
    evidence = item["evidence_ids"]
    if not isinstance(evidence, list) or not evidence:
        problems.append("evidence_ids must name at least one verbatim Evidence node")
    elif len(evidence) > MAX_EVIDENCE:
        problems.append(f"evidence_ids may name at most {MAX_EVIDENCE} Evidence nodes")
    elif len(set(map(str, evidence))) != len(evidence):
        problems.append("evidence_ids must be unique")
    else:
        problems.extend(f"evidence id {value!r} is not an evidence URN"
                        for value in evidence if not _urn(value, "evidence"))
    limits = item["limits"]
    if not isinstance(limits, list) or not all(_text(value) for value in limits):
        problems.append("limits must be a list of non-empty strings (empty when none)")
    if node_type == "Pattern":
        cases = item["case_ids"]
        if (not isinstance(cases, list) or len(cases) < 2 or len(set(map(str, cases))) != len(cases)
                or not all(_urn(value, "case") for value in cases)):
            problems.append("case_ids must name at least two distinct Case URNs")
    if node_type == "Domain":
        if not isinstance(item["domain_id"], str) or not SAFE_ID.fullmatch(item["domain_id"]):
            problems.append("domain_id must be a lowercase identifier")
        if not _text(item["public_label"]):
            problems.append("public_label must be non-empty text")
    return problems


# Card answers -------------------------------------------------------------


def card_store_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    return Path(os.environ.get(CARD_STORE_ENV) or DEFAULT_CARD_STORE).expanduser()


def read_card(path: Path, number: int, packet_sha: str) -> tuple[dict[str, Any], str]:
    """Return the card and the card store's sha256. The store is only read."""
    raw = _read_input(path, "card store", MAX_CARD_STORE_BYTES)
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refusal([f"card store is not UTF-8 JSON ({type(exc).__name__})"]) from exc
    cards = state.get("cards") if isinstance(state, dict) else None
    if not isinstance(cards, list):
        raise Refusal(["card store has no cards list"])
    matches = [card for card in cards if isinstance(card, dict)
               and _number(card.get("n")) and card["n"] == number]
    if len(matches) != 1:
        raise Refusal([f"card {number} is not in the card store" if not matches
                       else f"card {number} appears {len(matches)} times in the card store"])
    card = matches[0]
    problems = []
    if card.get("kind") not in {"ask", "decision"}:
        problems.append(f"card {number} is a {card.get('kind')!r} card, not an ask")
    if card.get("killed_by") == "owner" or card.get("withdrawn_at"):
        problems.append(f"card {number} was withdrawn by its owner")
    named = [card.get("review_packet_sha256"), card.get("what"), *(card.get("objects") or [])]
    if not any(isinstance(value, str) and packet_sha in value for value in named):
        problems.append(f"card {number} does not name this packet's sha256 {packet_sha} "
                        "in review_packet_sha256, what, or objects")
    if problems:
        raise Refusal(problems)
    return card, _sha256(raw)


def _origin(entry: dict[str, Any]) -> dict[str, Any]:
    return entry.get("origin") if isinstance(entry.get("origin"), dict) else {}


def _answer_records(card: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    records = [("answer", card["answer"])] if isinstance(card.get("answer"), dict) else []
    records += [("chat_answers", entry) for entry in card.get("chat_answers") or [] if isinstance(entry, dict)]
    answers = card.get("item_answers")
    if isinstance(answers, dict):
        records += [("item_answers", entry) for entry in answers.values() if isinstance(entry, dict)]
    return records


def flagged_proofs(card: dict[str, Any], number: int) -> list[str]:
    """A saved message the card program found edited or gone voids the card's answers."""
    return [f"card {number}: the saved message behind {record} {_origin(entry).get('address')} is "
            f"{_origin(entry)['flag'].get('result')}; re-ask on a new card"
            for record, entry in _answer_records(card)
            if isinstance(_origin(entry).get("flag"), dict)
            and _origin(entry)["flag"].get("result") != "intact"]


def recorded_lines(card: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Card-level answer lines the card program recorded, plus records that cannot count.

    Per-item batches (``batch_sha256``) are read through ``item_answers`` instead,
    where the card program keeps each item's latest answer.
    """
    batches = [entry for entry in card.get("chat_answers") or []
               if isinstance(entry, dict) and entry.get("batch_sha256")]
    batch_refs = {entry.get("ref") for entry in batches}
    lines, skipped = [], []
    for record, entry in _answer_records(card):
        if record == "item_answers" or entry.get("batch_sha256"):
            continue
        if record == "answer" and (entry.get("ref") in batch_refs or entry.get("reason") == "per-item answers"):
            continue
        text = entry.get("text") if record == "answer" else _unquote(entry.get("proof"))
        found = {"record": record, "at": entry.get("at"),
                 "address": _origin(entry).get("address") or entry.get("ref")}
        if not isinstance(text, str) or not text.strip():
            continue
        if entry.get("channel") == "mail":
            skipped.append({**found, "line": text.strip(), "why": "a mail reply is never an answer"})
            continue
        if (record == "chat_answers" or entry.get("source") == "chat") and not _origin(entry).get("address"):
            skipped.append({**found, "line": text.strip(), "why": "a chat answer without its native origin"})
            continue
        lines.extend({**found, "line": " ".join(raw.split())} for raw in text.splitlines() if raw.strip())
    return lines, skipped


def parse_answer(line: str, number: int, item_count: int) -> tuple[list[tuple[int, str, str]], str | None, str | None]:
    """Return (answers, why the line does not count, refusal) for one recorded line."""
    match = ITEM_ANSWER.match(line)
    if match:
        if int(match["n"]) != number:
            return [], f"names card {match['n']}", None
        item = int(match["item"])
        if not 1 <= item <= item_count:
            return [], None, f"answer {line!r} names item {item}, but the packet has {item_count} item(s)"
        verb, note, why = _verb(match["verb"], match["rest"])
        return ([(item, verb, note)], None, None) if why is None else ([], why, None)
    match = ALL_YES.match(line)
    if match:
        if int(match["n"]) != number:
            return [], f"names card {match['n']}", None
        return [(item, "yes", "") for item in range(1, item_count + 1)], None, None
    match = BARE_ANSWER.match(line)
    if match:
        if int(match["n"]) != number:
            return [], f"names card {match['n']}", None
        verb = match["verb"].lower()
        if item_count != 1 and verb == "yes":
            return [], (f"a bare yes counts only on a single-item card; "
                        f"answer each item as {number}.<item> yes|no|fix"), None
        if item_count != 1:
            return [], None, (f"card {number} has a card-level {verb} ({line!r}) on a multi-item card; "
                              "ask per item on a new card")
        verb, note, why = _verb(verb, match["rest"])
        return ([(1, verb, note)], None, None) if why is None else ([], why, None)
    return [], "not an answer in the ratification grammar", None


def _verb(verb: str, rest: str) -> tuple[str, str, str | None]:
    verb = verb.lower()
    note = rest.lstrip(" \t:,.!-").strip()
    if verb == "yes":
        return verb, "", ("a yes stands alone" if WORD.search(rest) else None)
    if verb == "fix" and not WORD.search(note):
        return verb, note, "a fix needs its note"
    return verb, note, None


def item_answers(card: dict[str, Any], number: int, statements: list[str]) -> tuple[list[tuple[int, str, str, dict]], list[str]]:
    """Per-item answers the card program recorded against the card's ratification_items."""
    if "ratification_items" not in card:
        return [], ([f"card {number} has item_answers but no ratification_items"]
                    if card.get("item_answers") else [])
    if card["ratification_items"] != statements:
        return [], [f"card {number} lists ratification_items that differ from the packet's statements"]
    items_sha = _text_sha256(json.dumps(statements, ensure_ascii=False, separators=(",", ":")))
    batches = {entry["batch_sha256"]: entry for entry in card.get("chat_answers") or []
               if isinstance(entry, dict) and isinstance(entry.get("batch_sha256"), str)}
    recorded = card.get("item_answers") or {}
    if not isinstance(recorded, dict):
        return [], [f"card {number} item_answers is not an object"]
    found, problems = [], []
    for key, entry in sorted(recorded.items(), key=lambda pair: str(pair[0])):
        where = f"card {number} item answer {key}"
        if (not isinstance(entry, dict) or not str(key).isdigit() or entry.get("item") != int(key)
                or not 1 <= int(key) <= len(statements)):
            problems.append(f"{where} is malformed")
            continue
        item, verb, note = int(key), entry.get("verb"), (entry.get("reason") or "").strip()
        if verb not in ACTIONS or (verb == "fix" and not WORD.search(note)):
            problems.append(f"{where} is not a yes, a no, or a fix with its note")
            continue
        if verb == "yes" and WORD.search(note):
            problems.append(f"{where} is a yes followed by words; a yes stands alone")
            continue
        if not _origin(entry).get("address"):
            problems.append(f"{where} lacks its native origin")
            continue
        if (entry.get("statement_sha256"), entry.get("packet_sha256")) != (
                _text_sha256(statements[item - 1]), items_sha):
            problems.append(f"{where} was recorded against a different statement")
            continue
        batch = batches.get(entry.get("batch_sha256"))
        typed = _unquote(batch.get("proof")) if batch else None
        if typed is None or _text_sha256(typed) != entry.get("batch_sha256"):
            problems.append(f"{where} is not backed by the saved message it was recorded from")
            continue
        matching = [line for line in (" ".join(raw.split()) for raw in typed.splitlines()) if line
                    and (item, verb, _key(note)) in {(i, v, _key(n)) for i, v, n in
                                                     parse_answer(line, number, len(statements))[0]}]
        if not matching:
            problems.append(f"{where} does not match the saved message it was recorded from")
            continue
        found.append((item, verb, note, {"record": "item_answers", "at": entry.get("at"),
                                         "address": _origin(entry).get("address"), "line": matching[0]}))
    return found, problems


def decide(card: dict[str, Any], number: int, statements: list[str]) -> tuple[dict[int, Decision], list[dict[str, Any]]]:
    """Map every item to exactly one recorded decision, or refuse."""
    count = len(statements)
    problems = flagged_proofs(card, number)
    if count > 1 and card.get("status") == "killed" and card.get("killed_by") == "anthony":
        problems.append(f"card {number} was killed by a card-level no; ask per item on a new card")
    found: dict[int, dict[tuple[str, str], Decision]] = {}

    def add(item: int, verb: str, note: str, record: dict[str, Any]) -> None:
        slot = found.setdefault(item, {})
        slot.setdefault((verb, _key(note)), Decision(item, verb, note)).answers.append(record)

    structured, structured_problems = item_answers(card, number, statements)
    problems.extend(structured_problems)
    for item, verb, note, record in structured:
        add(item, verb, note, record)
    lines, not_counted = recorded_lines(card)
    for record in lines:
        answers, why, problem = parse_answer(record["line"], number, count)
        if problem:
            problems.append(problem)
        elif why:
            not_counted.append({**record, "why": why})
        for item, verb, note in answers:
            add(item, verb, note, record)
    for item in sorted(found):
        if len(found[item]) > 1:
            problems.append(f"card {number} item {item} has conflicting answers: " + "; ".join(
                repr(decision.answers[0]["line"]) for decision in found[item].values()))
    unanswered = [str(item) for item in range(1, count + 1) if item not in found]
    if unanswered:
        problems.append(f"card {number} has no recorded answer for item(s) {', '.join(unanswered)}")
    if problems:
        problems.extend(f"not counted: {entry['line']!r} ({entry['why']})" for entry in not_counted)
        raise Refusal(problems)
    return {item: next(iter(slot.values())) for item, slot in found.items()}, not_counted


# Store --------------------------------------------------------------------


def store_identity(root: Path) -> tuple[Path, str]:
    database, identity = root / "imprint.db", root / "identity.json"
    if not database.is_file():
        raise Refusal([f"no Imprint store at {database}; this tool never creates one"])
    try:
        operator_id = json.loads(identity.read_text(encoding="utf-8")).get("operator_id")
    except (OSError, ValueError, AttributeError) as exc:
        raise Refusal([f"no readable operator identity at {identity}"]) from exc
    if not _urn(operator_id, "operator"):
        raise Refusal([f"operator identity at {identity} is not an operator URN"])
    return database, operator_id


@contextmanager
def read_only(database: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"{database.resolve(strict=True).as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _evidence_problem(conn: sqlite3.Connection, evidence_id: str, operator_id: str) -> str | None:
    """The reader's own test: current captured verbatim Evidence with a matching receipt."""
    rows = conn.execute("""
      SELECT n.node_type,n.operator_id,nv.payload_json,nv.provenance_status,nv.authority_tier,
             nv.valid_to,nv.event_id
      FROM nodes n JOIN node_versions nv USING(node_id)
      WHERE n.node_id=? AND nv.system_to IS NULL
    """, (evidence_id,)).fetchall()
    if len(rows) != 1:
        return "is not in the store"
    row = rows[0]
    if row["node_type"] != "Evidence" or row["operator_id"] != operator_id:
        return "is not Evidence of the configured operator"
    if (row["provenance_status"], row["authority_tier"], row["valid_to"]) != ("captured", "captured_judgment", None):
        return "is not current captured evidence"
    payload = json.loads(row["payload_json"])
    content, locator, digest = payload.get("content"), payload.get("source_locator"), payload.get("content_sha256")
    if payload.get("kind") != "operator_verbatim":
        return "is not the operator's verbatim words"
    if not _text(content) or not _text(locator) or _text_sha256(content) != digest:
        return "has missing or altered content"
    receipts = conn.execute(
        "SELECT kind,locator,content_sha256,event_id FROM source_receipts WHERE source_id=?", (evidence_id,)
    ).fetchall()
    if len(receipts) != 1 or tuple(receipts[0]) != ("operator_verbatim", locator, digest, row["event_id"]):
        return "has no matching source receipt"
    return None


def _case_problem(conn: sqlite3.Connection, case_id: str, operator_id: str) -> str | None:
    row = conn.execute("""
      SELECT n.node_type,n.operator_id FROM nodes n JOIN node_versions nv USING(node_id)
      WHERE n.node_id=? AND nv.system_to IS NULL
    """, (case_id,)).fetchone()
    if row is None or row["node_type"] != "Case" or row["operator_id"] != operator_id:
        return "is not a current Case of the configured operator"
    return None


def node_id_for(node_type: str, operator_id: str, packet_sha: str, item: int) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"imprint-ratify:{operator_id}:{packet_sha}:{item}")
    return f"urn:imprint:{node_type.lower()}:{value}"


def _contract(item: dict[str, Any], node_id: str, operator_id: str, model: str) -> dict[str, Any]:
    node_type, statement = item["node_type"], item["statement"]
    if node_type == "Domain":
        payload = {"domain_id": item["domain_id"], "public_label": item["public_label"],
                   "description": statement, "selected": False, "frozen": False}
    elif node_type == "Pattern":
        payload = {"statement": statement, "case_ids": list(item["case_ids"]),
                   "reason": None, "reason_status": "absent"}
    else:
        payload = {"statement": statement, "reason": None, "reason_status": "absent"}
    actor = uuid.uuid5(uuid.NAMESPACE_URL, f"imprint-ratify-model:{model}")
    return {
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION, "node_id": node_id,
        "node_type": node_type, "operator_id": operator_id, "payload": payload,
        "provenance": {
            "status": "inferred", "authority_tier": "inferred_candidate", "actor_class": "model",
            "actor_id": f"urn:imprint:model:{actor}", "mechanism": MECHANISM,
            "evidence_ids": list(item["evidence_ids"]), "model": model, "ratifier_id": None,
        },
    }


def _review_text(number: int, item: dict[str, Any], decision: Decision, packet_sha: str) -> str:
    where = f"card {number} item {item['item']} (packet sha256 {packet_sha})"
    if decision.verb == "yes":
        limits = "; ".join(item["limits"]) or "none stated"
        return f"Ratified on {where}. Answer: {decision.answers[0]['line']}. Limits: {limits}."
    if decision.verb == "no":
        return f"Declined on {where}" + (f": {decision.note}" if decision.note else ".")
    return f"Fix requested on {where}: {decision.note}"


def node_state(conn: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    """Classify a planned node by its versions and review events."""
    node = conn.execute("SELECT node_type,operator_id FROM nodes WHERE node_id=?", (node_id,)).fetchone()
    if node is None:
        return {"state": "absent"}
    versions = [dict(row) for row in conn.execute("""
      SELECT version_id,payload_sha256,provenance_status,authority_tier,provenance_json,event_id,system_to
      FROM node_versions WHERE node_id=? ORDER BY system_from,version_id
    """, (node_id,))]
    events = []
    for row in conn.execute("""
      SELECT event_id,event_type,payload_json FROM events WHERE payload_json LIKE ?
        AND event_type IN ('ratified','rejected','deferred','corrected','contested','tombstoned')
      ORDER BY system_time,event_id
    """, (f'%"node_id":"{node_id}"%',)):
        payload = json.loads(row["payload_json"])
        if payload.get("node_id") == node_id:
            events.append({"event_id": row["event_id"], "event_type": row["event_type"], "payload": payload})
    current = [version for version in versions if version["system_to"] is None]
    kinds = [event["event_type"] for event in events]
    state: dict[str, Any] = {"node_type": node["node_type"], "operator_id": node["operator_id"],
                             "payload_sha256s": sorted({version["payload_sha256"] for version in versions}),
                             "events": events}
    if not current and kinds == ["rejected"]:
        return {**state, "state": "rejected"}
    if len(current) == 1:
        head = current[0]
        if (head["provenance_status"], head["authority_tier"]) == ("inferred", "inferred_candidate") \
                and set(kinds) <= {"deferred"}:
            reasons = [event["payload"].get("reason", "") for event in events]
            return {**state, "state": "deferred" if reasons else "inferred", "deferrals": reasons}
        if (head["provenance_status"], head["authority_tier"]) == ("ratified", "ratified_knowledge") \
                and kinds == ["ratified"] and events[0]["event_id"] == head["event_id"]:
            return {**state, "state": "ratified", "head": head}
    return {**state, "state": "changed after review"}


def _steps(plan_verb: str, state: dict[str, Any], review_text: str) -> list[str] | None:
    """Store steps still owed for one item, or None when the store conflicts."""
    kind, action = state["state"], ACTIONS[plan_verb]
    if kind == "absent":
        return ["append", action]
    if kind == "inferred":
        return [action]
    if kind == FINAL_STATES[plan_verb] and kind != "deferred":
        return []
    if kind == "deferred" and plan_verb == "fix" and {_key(r) for r in state["deferrals"]} == {_key(review_text)}:
        return []
    return None


def plan_items(database: Path, operator_id: str, packet: dict[str, Any], packet_sha: str,
               decisions: dict[int, Decision]) -> list[ItemPlan]:
    plans, problems = [], []
    with read_only(database) as conn:
        for item in packet["items"]:
            number, decision = item["item"], decisions[item["item"]]
            item_problems = [f"item {number}: evidence {value} {why}" for value in item["evidence_ids"]
                             if (why := _evidence_problem(conn, value, operator_id))]
            item_problems += [f"item {number}: case {value} {why}" for value in item.get("case_ids", [])
                              if (why := _case_problem(conn, value, operator_id))]
            node_id = node_id_for(item["node_type"], operator_id, packet_sha, number)
            contract = _contract(item, node_id, operator_id, packet["model"])
            try:
                validate_node_contract(contract)
            except ValidationError as exc:
                item_problems.append(f"item {number}: the typed node would be invalid: {exc}")
            if item_problems:
                problems.extend(item_problems)
                continue
            review_text = _review_text(packet["card"], item, decision, packet_sha)
            state = node_state(conn, node_id)
            steps = _steps(decision.verb, state, review_text)
            if state["state"] != "absent" and (
                    state["node_type"] != item["node_type"] or state["operator_id"] != operator_id
                    or state["payload_sha256s"] != [payload_sha256(contract["payload"])]):
                steps = None
            if steps is None:
                problems.append(f"item {number}: node {node_id} is {state['state']} in the store, which "
                                f"conflicts with the recorded {decision.verb}; inspect it before any re-run")
                continue
            plans.append(ItemPlan(item, decision, node_id, contract, review_text, steps,
                                  pinned=decision.verb == "yes" and item["node_type"] != "Domain"))
    if problems:
        raise Refusal(problems)
    return plans


def current_pin(conn: sqlite3.Connection, node_id: str, operator_id: str) -> dict[str, str]:
    state = node_state(conn, node_id)
    if state["state"] != "ratified" or state["operator_id"] != operator_id:
        raise ConflictError(f"{node_id} is not ratified by the configured operator")
    head = state["head"]
    provenance = json.loads(head["provenance_json"])
    if provenance.get("actor_id") != operator_id or provenance.get("ratifier") != operator_id:
        raise ConflictError(f"{node_id} was not ratified by the configured operator")
    return {"node_id": node_id, "version_id": head["version_id"], "payload_sha256": head["payload_sha256"],
            "operator_id": operator_id, "ratification_event_id": head["event_id"]}


# Scope-bindings manifests -------------------------------------------------


def _casefold_path(path: Path) -> str:
    text = str(path)
    return text.casefold() if sys.platform in {"darwin", "win32"} else text


def out_root_path(value: Path) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise Refusal(["--out-root must be an absolute path"])
    resolved = root.resolve()
    home = Path.home().resolve()
    synced = [(home / name).resolve() for name in SYNCED_HOME_DIRS]
    candidates = [resolved, *resolved.parents]
    inside = any(_casefold_path(candidate) == _casefold_path(folder) for candidate in candidates for folder in synced)
    inside = inside or any(candidate.exists() and folder.exists() and os.path.samefile(candidate, folder)
                           for candidate in candidates for folder in synced)
    if _casefold_path(resolved) in {_casefold_path(home), _casefold_path(Path(resolved.anchor))} or inside or any(
            marker.casefold() in str(resolved).casefold() for marker in SYNC_MARKERS):
        raise Refusal([f"--out-root {root} is the home folder or syncs off this machine"])
    return resolved


def path_part(value: str) -> str:
    """Encode one scope value as a reversible, case-safe file name part."""
    encoded = "".join(char if char in PATH_SAFE else "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
                      for char in value)
    return encoded if len(encoded) <= 120 else f"~{_text_sha256(value)}"


def manifest_path(out_root: Path, owner: str, scope: dict[str, str]) -> Path:
    return (out_root / "scope-bindings" / owner / path_part(scope["client_id"])
            / path_part(scope["project_id"]) / f"{path_part(scope['domain'])}.json")


def load_manifest(path: Path, owner: str, operator_id: str, scope: dict[str, str]) -> tuple[list[dict[str, str]], str | None]:
    """Existing pins and the manifest's sha256 (None when it does not exist yet)."""
    sha = _file_sha256(path)
    if sha is None:
        return [], None
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refusal([f"manifest {path} is not UTF-8 JSON"]) from exc
    bindings = manifest.get("bindings") if isinstance(manifest, dict) else None
    if (len(raw) > MAX_MANIFEST_BYTES or not isinstance(manifest, dict) or set(manifest) != set(MANIFEST_KEYS)
            or manifest["schema"] != BINDINGS_SCHEMA or manifest["binding_kind"] != BINDINGS_KIND
            or not isinstance(bindings, list)
            or not all(isinstance(pin, dict) and set(pin) == set(PIN_KEYS) for pin in bindings)
            or len({str(pin["node_id"]) for pin in bindings}) != len(bindings)):
        raise Refusal([f"manifest {path} is not a valid {BINDINGS_SCHEMA} manifest"])
    if (manifest["owner"], manifest["operator_id"], manifest["scope"]) != (owner, operator_id, scope):
        raise Refusal([f"manifest {path} belongs to another owner, operator, or scope"])
    return bindings, sha


def plan_manifests(out_root: Path, database: Path, operator_id: str, plans: list[ItemPlan]) -> dict[Path, dict[str, Any]]:
    """Group pinned items by manifest and check every pin already in each one."""
    groups: dict[Path, dict[str, Any]] = {}
    for plan in plans:
        if plan.pinned:
            path = manifest_path(out_root, plan.item["owner"], plan.item["scope"])
            group = groups.setdefault(path, {"owner": plan.item["owner"], "scope": dict(plan.item["scope"]),
                                             "plans": []})
            group["plans"].append(plan)
    problems = []
    with read_only(database) as conn:
        for path, group in groups.items():
            group["existing"], group["sha256"] = load_manifest(path, group["owner"], operator_id, group["scope"])
            pending = {plan.node_id for plan in group["plans"] if plan.steps}
            for pin in group["existing"]:
                if pin["node_id"] in pending:
                    problems.append(f"{path} already pins {pin['node_id']}, which is not ratified yet")
                    continue
                try:
                    live = current_pin(conn, pin["node_id"], operator_id)
                except ConflictError:
                    live = None
                if pin != live:
                    problems.append(f"{path} holds a pin for {pin['node_id']} that no longer matches the store; "
                                    "repair the manifest before adding to it")
            known = {pin["node_id"] for pin in group["existing"]}
            added = sum(plan.node_id not in known for plan in group["plans"])
            if len(group["existing"]) + added > MAX_PINS:
                problems.append(f"{path} would hold more than {MAX_PINS} pins")
    if problems:
        raise Refusal(problems)
    return groups


def write_json_atomic(path: Path, value: Any, expected_sha256: str | None) -> str:
    """Replace path after re-checking its preimage; return the sha256 read back."""
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    secure_directory(path.parent)
    if _file_sha256(path) != expected_sha256:
        raise ConflictError(f"{path} changed while the batch ran")
    fd, temporary = tempfile.mkstemp(prefix=".ratify-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(Path(temporary))
        if _file_sha256(path) != expected_sha256:
            raise ConflictError(f"{path} changed while the batch ran")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    written = _file_sha256(path)
    if written != _sha256(data):
        raise ConflictError(f"{path} did not read back as written")
    return written


# Readback -----------------------------------------------------------------


def reader_file(value: Path) -> tuple[Path, str]:
    path = Path(value).expanduser()
    if not path.is_file():
        raise Refusal([f"--reader is not a readable file: {path}"])
    return path.resolve(), _sha256(path.read_bytes())


def run_reader(reader: Path, database: Path, scope: dict[str, str], bindings: Path | None) -> dict[str, Any]:
    """One read-only reader call with an empty query; Atlas sources are absent paths."""
    with tempfile.TemporaryDirectory(prefix="imprint-ratify-") as scratch:
        command = [
            sys.executable, "-I", "-B", "-X", "utf8", str(reader),
            "--client-id", scope["client_id"], "--project-id", scope["project_id"],
            "--domain", scope["domain"], "--query", "", "--limit", str(READER_LIMIT),
            "--imprint-db", str(database),
            "--atlas-candidates-db", str(Path(scratch) / "absent-atlas-candidates.db"),
            "--atlas-ledger-db", str(Path(scratch) / "absent-atlas-ledger.db"),
        ]
        if bindings is not None:
            command += ["--scope-bindings", str(bindings)]
        try:
            completed = subprocess.run(command, cwd=scratch, capture_output=True, encoding="utf-8",
                                       timeout=READER_TIMEOUT_SECONDS, check=False)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ConflictError(f"reader could not run: {type(exc).__name__}") from exc
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        result = None
    if completed.returncode != 0 or not isinstance(result, dict):
        detail = (completed.stdout or completed.stderr).strip()[:400]
        raise ConflictError(f"reader exited {completed.returncode}: {detail}")
    return result


def readback(reader: Path, database: Path, path: Path, scope: dict[str, str], expected: list[str]) -> dict[str, Any]:
    bound = run_reader(reader, database, scope, path)
    unbound = run_reader(reader, database, scope, None)
    bindings = bound.get("scope_bindings") if isinstance(bound.get("scope_bindings"), dict) else {}
    bound_claims = sorted(str(value) for value in bindings.get("bound_claims") or [])
    rejected = bindings.get("rejected") or []
    unbound_claims = unbound.get("claims") or []
    problems = []
    if bindings.get("status") != "bound":
        problems.append(f"bound read status is {bindings.get('status')!r}, not 'bound'")
    if rejected:
        problems.append(f"reader rejected {len(rejected)} pin(s) in {path}")
    missing = sorted(set(expected) - set(bound_claims))
    if missing:
        problems.append(f"bound read did not return {', '.join(missing)}")
    if unbound_claims:
        problems.append(f"unbound read returned {len(unbound_claims)} claim(s), expected 0")
    return {"manifest": str(path), "scope": scope, "bound_status": bindings.get("status"),
            "bound_claims": bound_claims, "expected_claims": sorted(expected), "rejected": rejected,
            "unbound_claim_count": len(unbound_claims), "bound_result_sha256": bound.get("result_sha256"),
            "unbound_result_sha256": unbound.get("result_sha256"), "problems": problems}


# Apply --------------------------------------------------------------------


def _perform(store: ImprintStore, plan: ItemPlan, step: str, operator_id: str) -> dict[str, str]:
    if step == "append":
        store.append_semantic_node(plan.contract, valid_from=utc_now())
        with store.connect() as conn:
            event_id = conn.execute("SELECT created_event_id FROM nodes WHERE node_id=?",
                                    (plan.node_id,)).fetchone()[0]
    elif step == "ratify":
        event_id = store.ratify_node(plan.node_id, ratifier=operator_id, note=plan.review_text)
    elif step == "reject":
        event_id = store.reject_node(plan.node_id, rejector=operator_id, reason=plan.review_text)
    else:
        event_id = store.defer_node(plan.node_id, reviewer=operator_id, reason=plan.review_text)
    return {"item": plan.item["item"], "operation": step, "node_id": plan.node_id, "event_id": event_id}


def _item_record(conn: sqlite3.Connection, plan: ItemPlan, pins: dict[str, dict[str, str]]) -> dict[str, Any]:
    state = node_state(conn, plan.node_id)
    created = conn.execute("SELECT created_event_id FROM nodes WHERE node_id=?", (plan.node_id,)).fetchone()
    item = plan.item
    return {
        "item": item["item"], "node_type": item["node_type"], "owner": item["owner"],
        "scope": item["scope"], "statement": item["statement"], "limits": item["limits"],
        "evidence_ids": item["evidence_ids"], "decision": plan.decision.verb, "note": plan.decision.note,
        "answers": plan.decision.answers, "node_id": plan.node_id, "state": state["state"],
        "candidate_event_id": created[0] if created else None,
        "review_events": [{"event_id": event["event_id"], "event_type": event["event_type"]}
                          for event in state.get("events", [])],
        "pin": pins.get(plan.node_id),
        "pin_status": ("pinned" if plan.node_id in pins else
                       "not pinned: the reader does not bind Domain nodes"
                       if plan.decision.verb == "yes" else None),
    }


def _prior_receipt(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def apply_packet(*, root: Path, packet_path: Path, out_root: Path, reader_path: Path,
                 card_store: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Validate everything, then apply a batch. Refusals raise ``Refusal`` before any write."""
    started_at = utc_now()
    out_root = out_root_path(out_root)
    reader, reader_sha = reader_file(reader_path)
    packet, packet_sha = load_packet(packet_path)
    number = packet["card"]
    database, operator_id = store_identity(Path(root))
    cards = card_store_path(card_store)
    card, card_store_sha = read_card(cards, number, packet_sha)
    decisions, not_counted = decide(card, number, [item["statement"] for item in packet["items"]])
    plans = plan_items(database, operator_id, packet, packet_sha, decisions)
    groups = plan_manifests(out_root, database, operator_id, plans)
    receipt_path = out_root / "receipts" / f"card-{number}-{packet_sha[:16]}.json"
    summary = {
        "card": number, "packet_sha256": packet_sha, "receipt": str(receipt_path),
        "decisions": {str(plan.item["item"]): plan.decision.verb for plan in plans},
        "not_counted": not_counted,
    }
    if dry_run:
        return {"status": "dry_run", **summary,
                "pending": [{"item": plan.item["item"], "node_id": plan.node_id, "steps": plan.steps}
                            for plan in plans if plan.steps],
                "manifests": sorted(str(path) for path in groups)}

    store = ImprintStore(database, expected_operator_id=operator_id)
    performed: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    pins: dict[str, dict[str, str]] = {}
    checks: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    error, status = None, None
    try:
        for plan in plans:
            for step in plan.steps:
                performed.append(_perform(store, plan, step, operator_id))
        with read_only(database) as conn:
            for group in groups.values():
                for plan in group["plans"]:
                    pins[plan.node_id] = current_pin(conn, plan.node_id, operator_id)
        for path, group in groups.items():
            known = {pin["node_id"] for pin in group["existing"]}
            added = [pins[plan.node_id] for plan in group["plans"] if plan.node_id not in known]
            sha = group["sha256"]
            if added:
                manifest = {"schema": BINDINGS_SCHEMA, "binding_kind": BINDINGS_KIND, "owner": group["owner"],
                            "operator_id": operator_id, "scope": group["scope"],
                            "bindings": [*group["existing"], *added]}
                sha = write_json_atomic(path, manifest, group["sha256"])
            manifests.append({"path": str(path), "owner": group["owner"], "scope": group["scope"],
                              "pins_added": len(added), "pin_count": len(known) + len(added), "sha256": sha})
    except Exception as exc:  # after the first write, every failure must still leave a receipt
        error, status = f"{type(exc).__name__}: {exc}", "partial"
    if status is None:
        try:
            for path, group in groups.items():
                checks.append(readback(reader, database, path, group["scope"],
                                       [plan.node_id for plan in group["plans"]]))
        except (ImprintError, OSError) as exc:
            error, status = f"{type(exc).__name__}: {exc}", "readback_failed"
    try:
        with read_only(database) as conn:
            items = [_item_record(conn, plan, pins) for plan in plans]
    except (ImprintError, OSError, sqlite3.Error) as exc:
        error = f"{error + '; ' if error else ''}item readback failed: {type(exc).__name__}: {exc}"
        status = status or "readback_failed"
    changed = bool(performed or any(entry["pins_added"] for entry in manifests))
    if status is None:
        if any(check["problems"] for check in checks):
            status = "readback_failed"
        else:
            status = "applied" if changed else "already_applied"
    recorded = "applied" if status == "already_applied" else status
    prior = _prior_receipt(receipt_path)
    receipt = {
        "schema": RECEIPT_SCHEMA, "status": recorded,
        "tool": {"name": "imprint-local", "version": PRODUCT_VERSION},
        "card": number, "packet": {"path": str(Path(packet_path).expanduser().resolve()), "sha256": packet_sha,
                                   "model": packet["model"], "items": len(packet["items"])},
        "card_store": {"path": str(cards), "sha256": card_store_sha},
        "imprint_db": str(database), "operator_id": operator_id,
        "reader": {"path": str(reader), "sha256": reader_sha},
        "not_counted": not_counted,
        "operations": [*(prior or {}).get("operations", []), *performed],
        "items": items, "manifests": manifests, "readback": checks, "error": error,
        "started_at": (prior or {}).get("started_at", started_at), "completed_at": utc_now(),
    }
    # The receipt records what was written. A re-run that writes nothing keeps it,
    # unless it is the first receipt or it completes an unfinished one.
    if prior is None or changed or (prior.get("status") != "applied" and recorded == "applied"):
        try:
            write_json_atomic(receipt_path, receipt, _file_sha256(receipt_path))
        except (ImprintError, OSError) as exc:
            status = "partial" if status in {"applied", "already_applied"} else status
            error = f"{error + '; ' if error else ''}receipt not written: {type(exc).__name__}: {exc}"
    return {"status": status, **summary, "changed": changed, "operations": len(performed),
            "manifests": manifests, "readback": [{"manifest": check["manifest"], "problems": check["problems"],
                                                  "bound_claims": len(check["bound_claims"]),
                                                  "unbound_claims": check["unbound_claim_count"]}
                                                 for check in checks],
            "error": error}
