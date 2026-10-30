"""Contract tests for ``imprint ratify apply``. Every item, card and store is synthetic."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from imprint.capture.schema import build_capture_envelope, new_urn
from imprint.cli import main
from imprint.errors import ImprintError
from imprint.ratify import Refusal, apply_packet, node_id_for, parse_answer, path_part
from imprint.store import ImprintStore


CARD = 9001
SCOPE = {"client_id": "example-client", "project_id": "example-project", "domain": "example-domain"}

# Stands in for the native judgment reader's CLI contract: it binds a pin only when the
# store's current head is the pinned ratified version with current evidence, and never
# reads unbound rows.
STUB_READER = r'''
import argparse, json, sqlite3, sys
from pathlib import Path

parser = argparse.ArgumentParser()
for flag in ("--client-id", "--project-id", "--domain", "--query", "--limit", "--imprint-db",
             "--atlas-candidates-db", "--atlas-ledger-db", "--scope-bindings"):
    parser.add_argument(flag)
args = parser.parse_args()
if args.query != "" or Path(args.atlas_candidates_db).exists() or Path(args.atlas_ledger_db).exists():
    print(json.dumps({"error": "unexpected reader input"}))
    sys.exit(2)
scope = {"client_id": args.client_id, "project_id": args.project_id, "domain": args.domain}
conn = sqlite3.connect(Path(args.imprint_db).resolve().as_uri() + "?mode=ro", uri=True)
result = {"scope": scope, "claims": []}
if args.scope_bindings:
    manifest = json.loads(Path(args.scope_bindings).read_text())
    if manifest["scope"] != scope:
        print(json.dumps({"error": "scope_bindings rejected"}))
        sys.exit(2)
    accepted, rejected = [], []
    for pin in manifest["bindings"]:
        row = conn.execute(
            "SELECT version_id,payload_sha256,provenance_status,event_id,payload_json,evidence_json"
            " FROM node_versions WHERE node_id=? AND system_to IS NULL", (pin["node_id"],)).fetchone()
        current = row and all(conn.execute(
            "SELECT 1 FROM node_versions WHERE node_id=? AND system_to IS NULL", (evidence,)).fetchone()
            for evidence in json.loads(row[5]))
        if current and list(row[:4]) == [pin["version_id"], pin["payload_sha256"], "ratified",
                                          pin["ratification_event_id"]]:
            accepted.append(pin["node_id"])
            result["claims"].append({"source": "imprint", "record_id": pin["node_id"],
                                     "text": json.loads(row[4])["statement"],
                                     "provenance": {"scope_source": "caller_scope_bindings"}})
        else:
            rejected.append({"node_id": pin["node_id"], "reasons": ["stub_pin_mismatch"]})
    result["scope_bindings"] = {"status": "bound" if accepted else "rejected", "rejected": rejected,
                                "bound_claims": accepted}
print(json.dumps(result))
'''


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Batch:
    """One synthetic Imprint store, card store, reader and receipts root."""

    def __init__(self, tmp_path: Path, reader: Path | None = None):
        self.tmp = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        data = tmp_path / "data"
        self.config = tmp_path / "config.json"
        self.config.write_text(json.dumps({"operator_slug": "ratify-test", "data_root": str(data)}))
        self.root = data / "ratify-test"
        self.store = ImprintStore(self.root / "imprint.db")
        self.store.initialize()
        self.operator = new_urn("operator")
        (self.root / "identity.json").write_text(json.dumps(
            {"identity_schema_version": "1.0.0", "operator_id": self.operator}))
        self.store.expected_operator_id = self.operator
        self.cards = tmp_path / "card-state" / "asks.json"
        self.out = tmp_path / "ratify-out"
        self.packet = tmp_path / "packet.json"
        self.statements: list[str] = []
        if reader is None:
            reader = tmp_path / "stub_reader.py"
            reader.write_text(STUB_READER)
        self.reader = reader

    def evidence(self, text: str, *, context: str | None = None) -> tuple[str, str, str | None]:
        envelope = build_capture_envelope(
            operator_id=self.operator, session_id=new_urn("session"), node_id="primary",
            case_description="Synthetic review of a draft", raw_operator_text=text,
            call_type="prefer", capture_mechanism="explicit_cli", captured_by="imprint-test",
            contextual_evidence=([{"kind": "context", "content": context,
                                   "source_locator": "synthetic:thread"}] if context else ()),
        )
        self.store.apply_capture(envelope)
        extra = envelope["evidence"][1]["evidence_id"] if context else None
        return envelope["evidence"][0]["evidence_id"], envelope["case"]["case_id"], extra

    def item(self, number: int, statement: str, evidence: list[str], *, node_type: str = "Principle",
             scope: dict | None = None, **extra) -> dict:
        return {"item": number, "owner": "example-owner", "node_type": node_type, "statement": statement,
                "scope": dict(scope or SCOPE), "evidence_ids": evidence,
                "limits": ["Synthetic drafts only."], **extra}

    def write_packet(self, items: list[dict], *, card: int = CARD) -> str:
        raw = json.dumps({"schema": "imprint-ratification-packet/v1", "card": card,
                          "model": "synthetic-model", "items": items}, indent=2).encode()
        self.packet.write_bytes(raw)
        self.statements = [item["statement"] for item in items]
        return hashlib.sha256(raw).hexdigest()

    def write_card(self, sha: str, *, batches: tuple = (), lines: tuple = (), card: int = CARD,
                   per_item: bool = True, **fields) -> dict:
        """Record answers the way the card program does: per-item batches and card-level lines."""
        record = {"n": card, "kind": "ask", "pen": "agent", "owner": "example-owner", "status": "out",
                  "what": "Ratify synthetic judgments.", "objects": [f"Ratification packet sha256 {sha}"],
                  "chat_answers": []}
        if per_item:
            record["ratification_items"] = list(self.statements)
        for index, text in enumerate(batches):
            self._record_batch(record, text, index)
        for index, line in enumerate(lines, start=len(batches)):
            address = f"t3:synthetic-thread:message-{index}"
            record["chat_answers"].append({
                "at": f"2026-10-28T12:{index:02d}:00", "source": "chat", "where": "fully-aware",
                "verb": line.split()[-1].lower(), "reason": "", "proof": f'"{line}"', "ref": address,
                "origin": {"address": address, "shape": "typed"}, "result": "recorded"})
        record.update(fields)
        self.cards.parent.mkdir(parents=True, exist_ok=True)
        self.cards.write_text(json.dumps({"next": card + 1, "cards": [record]}))
        return record

    def _record_batch(self, record: dict, text: str, index: int) -> None:
        statements = record["ratification_items"]
        items_sha = _sha(json.dumps(statements, ensure_ascii=False, separators=(",", ":")))
        address = f"t3:synthetic-thread:message-{index}"
        origin = {"address": address, "fingerprint": _sha(text), "shape": "typed"}
        actions = []
        for row in text.splitlines():
            one = re.fullmatch(r"([1-9]\d*)\.([1-9]\d*)\s+(yes|no|fix)(?:\s+(.*))?", row.strip(), re.I)
            if one:
                actions.append((int(one[2]), one[3].lower(), (one[4] or "").strip()))
            elif re.fullmatch(r"([1-9]\d*)\s+all\s+yes", row.strip(), re.I):
                actions += [(item, "yes", "") for item in range(1, len(statements) + 1)]
            else:
                actions.append((1, "yes", ""))
        answers = record.setdefault("item_answers", {})
        for item, verb, note in actions:
            answers[str(item)] = {
                "item": item, "verb": verb, "reason": note, "source": "chat", "where": "fully-aware",
                "at": f"2026-10-28T11:{index:02d}:00", "ref": address, "origin": dict(origin),
                "statement_sha256": _sha(statements[item - 1]), "packet_sha256": items_sha,
                "batch_sha256": _sha(text)}
        record["chat_answers"].append({
            "at": f"2026-10-28T11:{index:02d}:00", "source": "chat", "where": "fully-aware", "verb": "items",
            "reason": "", "proof": f'"{text}"', "ref": address, "origin": dict(origin),
            "batch_sha256": _sha(text), "packet_sha256": items_sha,
            "items": [{"item": item, "verb": verb, "reason": note} for item, verb, note in actions]})
        if len(answers) == len(statements):
            record["answer"] = {"at": "2026-10-28T11:59:00", "channel": "chat", "source": "chat",
                                "verb": "yes", "reason": "per-item answers", "text": text,
                                "proof": f'"{text}"', "ref": address, "origin": dict(origin)}

    def edit_card(self, change) -> None:
        state = json.loads(self.cards.read_text())
        change(state["cards"][0])
        self.cards.write_text(json.dumps(state))

    def run(self, **overrides) -> dict:
        arguments = {"root": self.root, "packet_path": self.packet, "out_root": self.out,
                     "reader_path": self.reader, "card_store": self.cards}
        return apply_packet(**{**arguments, **overrides})

    def refused(self, **overrides) -> list[str]:
        before = self.snapshot()
        with pytest.raises(Refusal) as refusal:
            self.run(**overrides)
        assert self.snapshot() == before
        return refusal.value.reasons

    def node(self, node_type: str, sha: str, item: int) -> str:
        return node_id_for(node_type, self.operator, sha, item)

    def manifest(self, owner: str = "example-owner", scope: dict = SCOPE) -> Path:
        return (self.out / "scope-bindings" / owner / path_part(scope["client_id"])
                / path_part(scope["project_id"]) / f"{path_part(scope['domain'])}.json")

    def heads(self) -> dict[str, dict]:
        return {row["node_id"]: row for row in self.store.current_nodes()}

    def snapshot(self) -> dict:
        with sqlite3.connect(self.root / "imprint.db") as conn:
            tables = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                      for name in ("events", "nodes", "node_versions", "source_receipts")}
        files = {str(path.relative_to(self.tmp)): path.read_bytes()
                 for path in sorted(self.out.rglob("*")) if path.is_file()} if self.out.exists() else {}
        return {"tables": tables, "files": files, "cards": self.cards.read_bytes() if self.cards.exists() else b""}


def _three_item_batch(batch: Batch) -> str:
    first, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    second, _, _ = batch.evidence("Do not book reviews on Fridays.")
    third, _, _ = batch.evidence("Say which source failed in every summary.")
    return batch.write_packet([
        batch.item(1, "Keep weekly status notes short.", [first]),
        batch.item(2, "Never book reviews on Fridays.", [second], node_type="Rule"),
        batch.item(3, "Summaries name the source that failed.", [third], node_type="Belief"),
    ])


MIXED = "9001.1 yes\n9001.2 NO not a standing rule\n9001.3 fix name the source first"


def test_mixed_packet_ratifies_rejects_and_defers(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=(MIXED,))
    cards_before = batch.cards.read_bytes()

    result = batch.run()

    assert (result["status"], result["changed"], result["error"]) == ("applied", True, None)
    assert result["decisions"] == {"1": "yes", "2": "no", "3": "fix"}
    yes, no, fix = batch.node("Principle", sha, 1), batch.node("Rule", sha, 2), batch.node("Belief", sha, 3)
    heads = batch.heads()
    assert (heads[yes]["provenance_status"], heads[yes]["authority_tier"]) == ("ratified", "ratified_knowledge")
    assert heads[yes]["provenance"]["ratifier"] == batch.operator
    candidate = batch.store.node_history(yes)["versions"][0]
    assert (candidate["authority_tier"], candidate["provenance"]["model"]) == ("inferred_candidate", "synthetic-model")
    assert candidate["evidence"] == json.loads(batch.packet.read_text())["items"][0]["evidence_ids"]
    assert no not in heads
    rejection = batch.store.node_history(no)["dispositions"]
    assert [event["event_type"] for event in rejection] == ["rejected"]
    assert rejection[0]["payload"]["reason"].endswith(": not a standing rule")
    assert heads[fix]["provenance_status"] == "inferred"
    deferral = batch.store.node_history(fix)["dispositions"]
    assert [event["event_type"] for event in deferral] == ["deferred"]
    assert deferral[0]["payload"]["reason"].endswith(": name the source first")

    manifest = json.loads(batch.manifest().read_text())
    assert (manifest["schema"], manifest["owner"], manifest["scope"]) == (
        "native-scope-bindings/v1", "example-owner", SCOPE)
    assert manifest["bindings"] == [{
        "node_id": yes, "version_id": manifest["bindings"][0]["version_id"],
        "payload_sha256": heads[yes]["payload_sha256"], "operator_id": batch.operator,
        "ratification_event_id": heads[yes]["event_id"],
    }]
    assert result["readback"] == [{"manifest": str(batch.manifest()), "problems": [],
                                   "bound_claims": 1, "unbound_claims": 0}]
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["status"] == "applied"
    assert [item["state"] for item in receipt["items"]] == ["ratified", "rejected", "deferred"]
    assert receipt["items"][0]["answers"][0]["line"] == "9001.1 yes"
    assert batch.cards.read_bytes() == cards_before


def test_rerun_of_the_same_packet_changes_nothing(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=(MIXED,))
    assert batch.run()["status"] == "applied"
    before = batch.snapshot()

    again = batch.run()

    assert (again["status"], again["changed"], again["operations"]) == ("already_applied", False, 0)
    assert again["readback"][0]["problems"] == []
    assert batch.snapshot() == before


def test_rerun_keeps_the_receipt_when_only_the_readback_changes(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=(MIXED,))
    receipt = Path(batch.run()["receipt"])
    before = batch.snapshot()
    leaky = tmp_path / "leaky_reader.py"
    leaky.write_text(STUB_READER.replace(
        'result = {"scope": scope, "claims": []}',
        'result = {"scope": scope, "claims": [] if args.scope_bindings else [{"record_id": "leak"}]}'))

    again = batch.run(reader_path=leaky)

    assert again["status"] == "readback_failed"
    assert again["readback"][0]["problems"] == ["unbound read returned 1 claim(s), expected 0"]
    assert batch.snapshot() == before
    assert json.loads(receipt.read_text())["status"] == "applied"

    broken = tmp_path / "broken_reader.py"
    broken.write_text("import sys\nprint('{}')\nsys.exit(2)\n")
    assert any("the reader could not check the pins" in reason for reason in batch.refused(reader_path=broken))


def test_interrupted_batch_resumes_to_the_same_end_state(tmp_path, monkeypatch):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=(MIXED,))

    def interrupted(self, node_id, **kwargs):
        raise ImprintError("simulated interruption")

    monkeypatch.setattr(ImprintStore, "reject_node", interrupted)
    first = batch.run()
    assert first["status"] == "partial" and "simulated interruption" in first["error"]
    assert not batch.manifest().exists()
    monkeypatch.undo()

    resumed = batch.run()

    assert (resumed["status"], resumed["changed"]) == ("applied", True)
    operations = json.loads(Path(resumed["receipt"]).read_text())["operations"]
    assert [(entry["item"], entry["operation"]) for entry in operations] == [
        (1, "append"), (1, "ratify"), (2, "append"), (2, "reject"), (3, "append"), (3, "defer")]
    assert batch.node("Rule", sha, 2) not in batch.heads()
    assert batch.run()["status"] == "already_applied"


def test_refuses_an_item_without_a_recorded_answer(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001.1 yes\n9001.3 no",))

    assert "card 9001 has no recorded answer for item(s) 2" in batch.refused()
    assert not batch.out.exists()


def test_counts_only_answers_the_card_program_recorded(tmp_path):
    batch = Batch(tmp_path)
    evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    sha = batch.write_packet([batch.item(1, "Keep weekly status notes short.", [evidence])])
    batch.write_card(sha, history=[{"event": "note", "note": "9001 yes"}], fix="9001 yes",
                     their_words={"quote": "9001 yes"},
                     unproven=[{"channel": "mail", "verb": "yes", "ref": "mail:synthetic"}],
                     chat_answers=[{"at": "2026-10-28T12:00:00", "source": "chat", "proof": '"9001 yes"'}])

    reasons = batch.refused()

    assert "card 9001 has no recorded answer for item(s) 1" in reasons
    assert any("without its native origin" in reason for reason in reasons)
    assert not batch.out.exists()


@pytest.mark.parametrize("defect, expected", [
    ("no evidence", "evidence_ids must name at least one verbatim Evidence node"),
    ("too much evidence", "evidence_ids may name at most 50 Evidence nodes"),
    ("unknown evidence", "is not in the store"),
    ("context evidence", "is not the operator's verbatim words"),
    ("no scope", "missing scope"),
    ("partial scope", "scope must have exactly client_id, project_id and domain"),
    ("padded scope", "scope.domain must be non-empty text without surrounding spaces"),
])
def test_refuses_missing_evidence_or_scope(tmp_path, defect, expected):
    batch = Batch(tmp_path)
    verbatim, _, context = batch.evidence("Keep weekly status notes to five lines.", context="Thread excerpt.")
    item = batch.item(1, "Keep weekly status notes short.", [verbatim])
    if defect == "no evidence":
        item["evidence_ids"] = []
    elif defect == "too much evidence":
        item["evidence_ids"] = [new_urn("evidence") for _ in range(51)]
    elif defect == "unknown evidence":
        item["evidence_ids"] = [new_urn("evidence")]
    elif defect == "context evidence":
        item["evidence_ids"] = [context]
    elif defect == "no scope":
        del item["scope"]
    elif defect == "partial scope":
        del item["scope"]["project_id"]
    else:
        item["scope"]["domain"] = " example-domain"
    batch.write_card(batch.write_packet([item]), batches=("9001 yes",))

    reasons = batch.refused()

    assert any(expected in reason for reason in reasons), reasons
    assert not batch.out.exists()


def test_refuses_text_that_cannot_be_stored(tmp_path):
    batch = Batch(tmp_path)
    evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    raw = json.dumps({"schema": "imprint-ratification-packet/v1", "card": CARD, "model": "synthetic-model",
                      "items": [batch.item(1, "Keep notes short.", [evidence])]})
    batch.packet.write_text(raw.replace("Keep notes short.", "Keep notes short \\ud800"))
    batch.write_card(hashlib.sha256(batch.packet.read_bytes()).hexdigest(), per_item=False, lines=("9001 yes",))

    assert any("packet is not UTF-8 JSON" in reason for reason in batch.refused())


def test_bare_yes_counts_only_on_a_single_item_card(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, per_item=False, lines=("9001 yes",))

    reasons = batch.refused()

    assert "card 9001 has no recorded answer for item(s) 1, 2, 3" in reasons
    assert any("a bare yes counts only on a single-item card" in reason for reason in reasons)

    single = Batch(tmp_path / "single")
    evidence, _, _ = single.evidence("Keep weekly status notes to five lines.")
    sha = single.write_packet([single.item(1, "Keep weekly status notes short.", [evidence])])
    single.write_card(sha, batches=("Card 9001 YES",))
    assert single.run()["status"] == "applied"
    assert single.heads()[single.node("Principle", sha, 1)]["provenance_status"] == "ratified"


def test_refuses_a_card_that_does_not_list_the_packet_statements(tmp_path):
    batch = Batch(tmp_path)
    evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    sha = batch.write_packet([batch.item(1, "Status notes may run to ten lines.", [evidence])])
    batch.write_card(sha, per_item=False, lines=("9001 yes",), draft="keep weekly status notes to five lines",
                     review_packet_sha256=sha)

    assert any("has no ratification_items" in reason for reason in batch.refused())
    batch.write_card(sha, per_item=False, kind="decision", sentence="Status notes may run to ten lines.",
                     answer={"at": "2026-10-28T13:00:00", "channel": "imessage", "proof": "his own number",
                             "ref": "imessage:synthetic", "text": "9001 yes"})
    assert any("has no ratification_items" in reason for reason in batch.refused())
    assert not batch.out.exists()


def test_card_level_no_or_fix_on_a_multi_item_card_refuses(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001 all yes",), lines=("9001 no",), status="killed", killed_by="anthony")
    reasons = batch.refused()
    assert "card 9001 was killed by a card-level no; ask per item on a new card" in reasons
    assert any("card-level no" in reason for reason in reasons)

    batch.write_card(sha, batches=("9001 all yes",), lines=("9001 fix item 2 is too broad",), status="edited")
    assert any("card-level fix" in reason for reason in batch.refused())

    batch.write_card(sha, batches=("9001 all yes",), status="killed", killed_by="anthony",
                     answer={"channel": "imessage", "text": "9001 no", "at": "2026-10-28T13:00:00"})
    assert len(batch.refused()) >= 2
    assert not batch.out.exists()


def test_latest_item_answer_wins_and_card_level_conflicts_refuse(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001.1 yes\n9001.2 fix shorter\n9001.3 yes", "9001.2 yes"))

    result = batch.run()

    assert result["status"] == "applied" and result["decisions"] == {"1": "yes", "2": "yes", "3": "yes"}
    assert result["readback"][0]["bound_claims"] == 3
    pinned = [pin["node_id"] for pin in json.loads(batch.manifest().read_text())["bindings"]]
    assert pinned == [batch.node("Principle", sha, 1), batch.node("Rule", sha, 2), batch.node("Belief", sha, 3)]

    single = Batch(tmp_path / "single")
    evidence, _, _ = single.evidence("Keep weekly status notes to five lines.")
    sha = single.write_packet([single.item(1, "Keep weekly status notes short.", [evidence])])
    single.write_card(sha, batches=("9001 yes",), lines=("9001 no",), status="killed", killed_by="anthony")
    assert any("item 1 has conflicting answers" in reason for reason in single.refused())


@pytest.mark.parametrize("count, text, fields", [
    (1, "9001 nope", {"status": "killed", "killed_by": "anthony"}),
    (1, "9001 kill", {"status": "killed", "killed_by": "anthony"}),
    (1, "9001 edit tighten wording", {"status": "edited", "his_words": "tighten wording"}),
    (3, "9001 change item 2 to weekly", {"status": "edited", "his_words": "change item 2 to weekly"}),
    (3, "9001 all yes", {"status": "edited", "his_words": "all yes"}),
    (3, "9001.2 no", {"status": "edited", "his_words": ".2 no"}),
])
def test_texted_card_level_answers_refuse_instead_of_being_skipped(tmp_path, count, text, fields):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch) if count == 3 else batch.write_packet(
        [batch.item(1, "Keep weekly status notes short.", [batch.evidence("Keep notes to five lines.")[0]])])
    history = [{"event": "killed" if "killed_by" in fields else "edit", "note": text}]
    batch.write_card(sha, batches=("9001 all yes" if count == 3 else "9001 yes",), history=history,
                     answer={"at": "2026-10-28T13:00:00", "channel": "imessage", "proof": "his own number",
                             "ref": "imessage:synthetic", "text": text}, **fields)

    reasons = batch.refused()

    assert any(text in reason or "card-level" in reason for reason in reasons), reasons
    assert not batch.out.exists()

    legacy = Batch(tmp_path / "legacy")
    sha = legacy.write_packet([legacy.item(1, "Keep weekly status notes short.",
                                           [legacy.evidence("Keep notes to five lines.")[0]])])
    legacy.write_card(sha, per_item=False, lines=("9001 yes",), history=history,
                      answer={"at": "2026-10-28T13:00:00", "channel": "imessage", "proof": "his own number",
                              "ref": "imessage:synthetic", "text": text}, **fields)
    assert legacy.refused()


def test_a_card_level_edit_the_card_program_dropped_still_refuses(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001.1 yes", "9001.2 yes\n9001.3 yes"), status="yes",
                     history=[{"event": "item 1 yes"}, {"event": "edit", "note": "fix item 1 overstates"},
                              {"event": "item 2 yes"}, {"event": "item 3 yes"}])

    assert any("history shows a card-level answer" in reason for reason in batch.refused())


def test_a_proof_flagged_on_another_card_voids_the_shared_message(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    record = batch.write_card(sha, batches=("9001 all yes",))
    sibling = json.loads(json.dumps(record))
    sibling["n"] = 9002
    for entry in [sibling["answer"], *sibling["chat_answers"], *sibling["item_answers"].values()]:
        entry["origin"]["flag"] = {"result": "tampered", "detail": "edited after recording"}
    batch.cards.write_text(json.dumps({"next": 9003, "cards": [record, sibling]}))

    assert any("flagged on another card" in reason for reason in batch.refused())


def test_card_program_item_rows_are_read_the_way_it_splits_them(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001.1 yes 👍\n9001.2 no - duplicate of 1\n9001.3 fix ...shorter",))

    result = batch.run()

    assert result["status"] == "applied" and result["decisions"] == {"1": "yes", "2": "no", "3": "fix"}
    deferral = batch.store.node_history(batch.node("Belief", sha, 3))["dispositions"][0]
    assert deferral["payload"]["reason"].endswith(": ...shorter")

    single = Batch(tmp_path / "single")
    evidence, _, _ = single.evidence("Keep weekly status notes to five lines.")
    sha = single.write_packet([single.item(1, "Keep weekly status notes short.", [evidence])])
    single.write_card(sha, batches=("9001. yes",))
    assert single.run()["status"] == "applied"


def test_a_later_yes_replaces_an_applied_fix(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    first = "9001.1 yes\n9001.2 fix shorter\n9001.3 yes"
    batch.write_card(sha, batches=(first,), status="edited")
    assert batch.run()["decisions"] == {"1": "yes", "2": "fix", "3": "yes"}

    batch.write_card(sha, batches=(first, "9001.2 yes"), status="yes")
    result = batch.run()

    assert (result["status"], result["decisions"]["2"]) == ("applied", "yes")
    rule = batch.node("Rule", sha, 2)
    assert batch.heads()[rule]["provenance_status"] == "ratified"
    assert [event["event_type"] for event in batch.store.node_history(rule)["dispositions"]] == ["deferred", "ratified"]
    assert result["readback"][0]["bound_claims"] == 3
    assert batch.run()["status"] == "already_applied"


def test_refuses_flagged_or_mismatched_item_answers(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)

    def flag(card):
        card["item_answers"]["2"]["origin"]["flag"] = {"result": "tampered", "detail": "edited"}

    def restate(card):
        card["item_answers"]["2"]["statement_sha256"] = _sha("A statement he never saw.")

    def unbacked(card):
        card["chat_answers"] = []

    def reverb(card):
        card["item_answers"]["2"]["verb"] = "no"

    def relist(card):
        card["ratification_items"][1] = "Book reviews on any day."

    for change, expected in ((flag, "is tampered"), (restate, "recorded against a different statement"),
                             (unbacked, "is not backed by the saved message"),
                             (reverb, "does not match the saved message"),
                             (relist, "ratification_items that differ from the packet's statements")):
        batch.write_card(sha, batches=("9001 all yes",))
        batch.edit_card(change)
        assert any(expected in reason for reason in batch.refused()), expected
    assert not batch.out.exists()


def test_refuses_a_card_that_does_not_show_this_packet(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001 all yes",), objects=["Some other packet"], what="Ratify something.")
    assert any("does not name this packet's sha256" in reason for reason in batch.refused())

    batch.write_card(sha, batches=("9001 all yes",), killed_by="owner", withdrawn_at="2026-10-28T13:00:00")
    assert "card 9001 was withdrawn by its owner" in batch.refused()
    assert not batch.out.exists()


def test_second_packet_appends_and_stale_pins_refuse_first(tmp_path):
    batch = Batch(tmp_path)
    first_evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    first = batch.write_packet([batch.item(1, "Keep weekly status notes short.", [first_evidence])])
    batch.write_card(first, batches=("9001 yes",))
    assert batch.run()["status"] == "applied"

    second_evidence, _, _ = batch.evidence("Lead every summary with the decision.")
    second = batch.write_packet([batch.item(1, "Summaries open with the decision.", [second_evidence])],
                                card=9002)
    batch.write_card(second, batches=("9002.1 yes",), card=9002)
    result = batch.run()

    assert result["status"] == "applied" and result["readback"][0]["bound_claims"] == 2
    pinned = [pin["node_id"] for pin in json.loads(batch.manifest().read_text())["bindings"]]
    assert pinned == [batch.node("Principle", first, 1), batch.node("Principle", second, 1)]

    batch.store.tombstone_node(batch.node("Principle", first, 1), reason="Synthetic retirement.")
    third_evidence, _, _ = batch.evidence("Put the ask in the first line.")
    third = batch.write_packet([batch.item(1, "Lead with the ask.", [third_evidence])], card=9003)
    batch.write_card(third, batches=("9003 yes",), card=9003)
    assert any("does not accept every pin already in" in reason for reason in batch.refused())
    assert any("does not accept every pin already in" in reason for reason in batch.refused(dry_run=True))


def test_inherited_pin_with_withdrawn_evidence_refuses_before_any_write(tmp_path):
    batch = Batch(tmp_path)
    first_evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    first = batch.write_packet([batch.item(1, "Keep weekly status notes short.", [first_evidence])])
    batch.write_card(first, batches=("9001 yes",))
    assert batch.run()["status"] == "applied"
    batch.store.tombstone_node(first_evidence, reason="Synthetic withdrawal.")

    second_evidence, _, _ = batch.evidence("Lead every summary with the decision.")
    second = batch.write_packet([batch.item(1, "Summaries open with the decision.", [second_evidence])],
                                card=9002)
    batch.write_card(second, batches=("9002 yes",), card=9002)

    assert any("does not accept every pin already in" in reason for reason in batch.refused())


def test_scope_values_the_reader_accepts_are_encoded_into_paths(tmp_path):
    batch = Batch(tmp_path)
    scope = {"client_id": "Example Client", "project_id": "ABC1-example", "domain": "web/../design"}
    evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    sha = batch.write_packet([batch.item(1, "Keep weekly status notes short.", [evidence], scope=scope)])
    batch.write_card(sha, batches=("9001 yes",))

    result = batch.run()

    assert result["status"] == "applied"
    path = batch.manifest(scope=scope)
    assert path.relative_to(batch.out).parts == (
        "scope-bindings", "example-owner", "%45xample%20%43lient", "%41%42%431-example", "web%2F%2E%2E%2Fdesign.json")
    assert json.loads(path.read_text())["scope"] == scope
    assert path_part("x" * 200).startswith("~")


def test_pattern_items_are_pinned_and_domain_items_are_not(tmp_path):
    batch = Batch(tmp_path)
    one, case_one, _ = batch.evidence("Short notes landed better in review.")
    two, case_two, _ = batch.evidence("The short note was read; the long one was not.")
    sha = batch.write_packet([
        batch.item(1, "Short notes get read.", [one, two], node_type="Pattern", case_ids=[case_one, case_two]),
        batch.item(2, "Status reporting for internal reviews.", [one], node_type="Domain",
                   domain_id="status-reporting", public_label="Status reporting"),
    ])
    batch.write_card(sha, batches=("9001 all yes",))

    result = batch.run()

    assert result["status"] == "applied"
    pattern, domain = batch.node("Pattern", sha, 1), batch.node("Domain", sha, 2)
    heads = batch.heads()
    assert heads[pattern]["provenance_status"] == heads[domain]["provenance_status"] == "ratified"
    assert heads[domain]["payload"]["selected"] is False
    assert [pin["node_id"] for pin in json.loads(batch.manifest().read_text())["bindings"]] == [pattern]
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["items"][1]["pin_status"] == "not pinned: the reader does not bind Domain nodes"


def test_dry_run_and_synced_receipt_roots_write_nothing(tmp_path, monkeypatch):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001 all yes",))
    before = batch.snapshot()

    plan = batch.run(dry_run=True)

    assert plan["status"] == "dry_run" and [entry["steps"] for entry in plan["pending"]] == [["append", "ratify"]] * 3
    assert batch.snapshot() == before and not batch.out.exists()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    synced = ["Documents/ratify", "Desktop", "Library/Mobile Documents/ratify", "Dropbox/ratify", "."]
    if sys.platform in {"darwin", "win32"}:
        synced.append("documents/ratify")
    for name in synced:
        batch.refused(out_root=home / name)
        assert not home.exists()
    batch.refused(out_root=Path("relative/ratify"))


def test_cli_requires_an_explicit_config_and_reports_refusals(tmp_path, capsys, monkeypatch):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=("9001.1 yes",))
    arguments = ["ratify", "apply", "--packet", str(batch.packet), "--out-root", str(batch.out),
                 "--reader", str(batch.reader)]
    monkeypatch.setenv("AGENTS_CARD_STORE", str(batch.cards))

    assert main(arguments) == 2
    assert "requires --config" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--config", str(batch.config), *arguments]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "refused" and "card 9001 has no recorded answer for item(s) 2, 3" in refused["reasons"]

    batch.write_card(sha, batches=("9001.1 yes\n9001.2 yes\n9001.3 yes",))
    assert main(["--config", str(batch.config), *arguments]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert main(["--config", str(batch.config), *arguments]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_applied"


@pytest.mark.parametrize("line, expected", [
    ("9001.2 yes", ([(2, "yes", "")], None, None)),
    ("9001.2 YES.", ([(2, "yes", "")], None, None)),
    ("card #9001.3: Fix tighten the opening", ([(3, "fix", "tighten the opening")], None, None)),
    ("9001.1 no", ([(1, "no", "")], None, None)),
    ("9001.1 no - not durable", ([(1, "no", "not durable")], None, None)),
    ("9001 All Yes", ([(1, "yes", ""), (2, "yes", ""), (3, "yes", "")], None, None)),
    ("9001.1 yes please", ([], "a yes stands alone", None)),
    ("9001.1 fix", ([], "a fix needs its note", None)),
    ("9001.1 yesterday", ([], "not an answer in the ratification grammar", None)),
    ("9002.1 yes", ([], "names card 9002", None)),
    ("9001 yes", ([], "a bare yes counts only on a single-item card; answer each item as 9001.<item> yes|no|fix",
                  None)),
    ("9001 no", ([], None, "card 9001 has a card-level no ('9001 no') on a multi-item card; ask per item on a new card")),
    ("9001.4 yes", ([], None, "answer '9001.4 yes' names item 4, but the packet has 3 item(s)")),
])
def test_answer_grammar(line, expected):
    assert parse_answer(line, CARD, 3) == expected


@pytest.mark.skipif(not os.environ.get("IMPRINT_RATIFY_READER"),
                    reason="set IMPRINT_RATIFY_READER to the native judgment reader to run")
def test_readback_through_the_native_reader(tmp_path):
    batch = Batch(tmp_path, reader=Path(os.environ["IMPRINT_RATIFY_READER"]))
    sha = _three_item_batch(batch)
    batch.write_card(sha, batches=(MIXED,))

    result = batch.run()

    assert result["status"] == "applied", result
    assert result["readback"] == [{"manifest": str(batch.manifest()), "problems": [],
                                   "bound_claims": 1, "unbound_claims": 0}]
    assert batch.run()["status"] == "already_applied"
