"""Contract tests for ``imprint ratify apply``. Every item, card and store is synthetic."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from imprint.capture.schema import build_capture_envelope, new_urn
from imprint.cli import main
from imprint.errors import ImprintError
from imprint.ratify import Refusal, apply_packet, node_id_for, parse_answer
from imprint.store import ImprintStore


CARD = 9001
SCOPE = {"client_id": "example-client", "project_id": "example-project", "domain": "example-domain"}

# Stands in for the native judgment reader's CLI contract: it binds a pin only when the
# store's current head is the pinned ratified version, and never reads unbound rows.
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
            "SELECT version_id,payload_sha256,provenance_status,event_id,payload_json FROM node_versions"
            " WHERE node_id=? AND system_to IS NULL", (pin["node_id"],)).fetchone()
        if row and list(row[:4]) == [pin["version_id"], pin["payload_sha256"], "ratified",
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
             **extra) -> dict:
        return {"item": number, "owner": "example-owner", "node_type": node_type, "statement": statement,
                "scope": dict(SCOPE), "evidence_ids": evidence,
                "limits": ["Synthetic drafts only."], **extra}

    def write_packet(self, items: list[dict], *, card: int = CARD) -> str:
        raw = json.dumps({"schema": "imprint-ratification-packet/v1", "card": card,
                          "model": "synthetic-model", "items": items}, indent=2).encode()
        self.packet.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def write_card(self, lines: list[str], sha: str, *, card: int = CARD, **fields) -> None:
        answers = [{
            "at": f"2026-10-28T12:{index:02d}:00", "source": "chat", "where": "fully-aware",
            "verb": "yes", "reason": "", "proof": f'"{line}"', "ref": f"t3:synthetic-thread:message-{index}",
            "origin": {"address": f"t3:synthetic-thread:message-{index}", "shape": "typed"},
            "result": "recorded",
        } for index, line in enumerate(lines)]
        record = {"n": card, "kind": "ask", "owner": "example-owner", "status": "out",
                  "what": "Ratify synthetic judgments.", "objects": [f"Ratification packet sha256 {sha}"],
                  "chat_answers": answers, **fields}
        self.cards.parent.mkdir(parents=True, exist_ok=True)
        self.cards.write_text(json.dumps({"next": card + 1, "cards": [record]}))

    def run(self, **overrides) -> dict:
        arguments = {"root": self.root, "packet_path": self.packet, "out_root": self.out,
                     "reader_path": self.reader, "card_store": self.cards}
        return apply_packet(**{**arguments, **overrides})

    def node(self, node_type: str, sha: str, item: int) -> str:
        return node_id_for(node_type, self.operator, sha, item)

    def manifest(self, owner: str = "example-owner", scope: dict = SCOPE) -> Path:
        return (self.out / "scope-bindings" / owner / scope["client_id"] / scope["project_id"]
                / f"{scope['domain']}.json")

    def heads(self) -> dict[str, dict]:
        return {row["node_id"]: row for row in self.store.current_nodes()}

    def snapshot(self) -> dict:
        with sqlite3.connect(self.root / "imprint.db") as conn:
            tables = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                      for name in ("events", "nodes", "node_versions", "source_receipts")}
        files = {str(path.relative_to(self.tmp)): path.read_bytes()
                 for path in sorted(self.out.rglob("*")) if path.is_file()} if self.out.exists() else {}
        return {"tables": tables, "files": files, "cards": self.cards.read_bytes()}


def _three_item_batch(batch: Batch) -> str:
    first, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    second, _, _ = batch.evidence("Do not book reviews on Fridays.")
    third, _, _ = batch.evidence("Say which source failed in every summary.")
    return batch.write_packet([
        batch.item(1, "Keep weekly status notes short.", [first]),
        batch.item(2, "Never book reviews on Fridays.", [second], node_type="Rule"),
        batch.item(3, "Summaries name the source that failed.", [third], node_type="Belief"),
    ])


def test_mixed_packet_ratifies_rejects_and_defers(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(["9001.1 yes", "9001.2 NO not a standing rule", "9001.3 fix name the source first"], sha)
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
    assert [event["event_type"] for event in batch.store.node_history(no)["dispositions"]] == ["rejected"]
    assert batch.store.node_history(no)["dispositions"][0]["payload"]["reason"].endswith(": not a standing rule")
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
    batch.write_card(["9001.1 yes", "9001.2 no", "9001.3 fix tighten the wording"], sha)
    assert batch.run()["status"] == "applied"
    before = batch.snapshot()

    again = batch.run()

    assert (again["status"], again["changed"], again["operations"]) == ("already_applied", False, 0)
    assert again["readback"][0]["problems"] == []
    assert batch.snapshot() == before


def test_interrupted_batch_resumes_to_the_same_end_state(tmp_path, monkeypatch):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(["9001.1 yes", "9001.2 no", "9001.3 fix tighten the wording"], sha)

    def interrupted(self, node_id, **kwargs):
        raise ImprintError("simulated interruption")

    monkeypatch.setattr(ImprintStore, "reject_node", interrupted)
    first = batch.run()
    assert first["status"] == "partial" and "simulated interruption" in first["error"]
    assert not batch.manifest().exists()
    monkeypatch.undo()

    resumed = batch.run()

    assert (resumed["status"], resumed["changed"]) == ("applied", True)
    assert [item["operation"] for item in json.loads(Path(resumed["receipt"]).read_text())["operations"]] == [
        "reject", "append", "defer"]
    assert batch.node("Rule", sha, 2) not in batch.heads()
    assert batch.run()["status"] == "already_applied"


def test_refuses_an_item_without_a_recorded_answer(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(["9001.1 yes", "9001.3 no"], sha)
    before = batch.snapshot()

    with pytest.raises(Refusal) as refused:
        batch.run()

    assert "card 9001 has no recorded answer for item(s) 2" in refused.value.reasons
    assert batch.snapshot() == before and not batch.out.exists()


def test_counts_only_answers_the_card_program_recorded(tmp_path):
    batch = Batch(tmp_path)
    evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    sha = batch.write_packet([batch.item(1, "Keep weekly status notes short.", [evidence])])
    batch.write_card([], sha, history=[{"event": "note", "note": "9001 yes"}], fix="9001 yes",
                     their_words={"quote": "9001 yes"},
                     unproven=[{"channel": "mail", "verb": "yes", "ref": "mail:synthetic"}])
    card = json.loads(batch.cards.read_text())
    card["cards"][0]["chat_answers"] = [{"at": "2026-10-28T12:00:00", "source": "chat", "proof": '"9001 yes"'}]
    batch.cards.write_text(json.dumps(card))

    with pytest.raises(Refusal) as refused:
        batch.run()

    assert "card 9001 has no recorded answer for item(s) 1" in refused.value.reasons
    assert any("without its native origin" in reason for reason in refused.value.reasons)
    assert not batch.out.exists()


@pytest.mark.parametrize("defect, expected", [
    ("no evidence", "evidence_ids must name at least one verbatim Evidence node"),
    ("unknown evidence", "is not in the store"),
    ("context evidence", "is not the operator's verbatim words"),
    ("no scope", "missing scope"),
    ("partial scope", "scope must have exactly client_id, project_id and domain"),
])
def test_refuses_missing_evidence_or_scope(tmp_path, defect, expected):
    batch = Batch(tmp_path)
    verbatim, _, context = batch.evidence("Keep weekly status notes to five lines.", context="Thread excerpt.")
    item = batch.item(1, "Keep weekly status notes short.", [verbatim])
    if defect == "no evidence":
        item["evidence_ids"] = []
    elif defect == "unknown evidence":
        item["evidence_ids"] = [new_urn("evidence")]
    elif defect == "context evidence":
        item["evidence_ids"] = [context]
    elif defect == "no scope":
        del item["scope"]
    else:
        del item["scope"]["project_id"]
    batch.write_card(["9001 yes"], batch.write_packet([item]))
    before = batch.snapshot()

    with pytest.raises(Refusal) as refused:
        batch.run()

    assert any(expected in reason for reason in refused.value.reasons), refused.value.reasons
    assert batch.snapshot() == before and not batch.out.exists()


def test_bare_yes_counts_only_on_a_single_item_card(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(["9001 yes"], sha)
    before = batch.snapshot()

    with pytest.raises(Refusal) as refused:
        batch.run()

    assert "card 9001 has no recorded answer for item(s) 1, 2, 3" in refused.value.reasons
    assert any("a bare yes counts only on a single-item card" in reason for reason in refused.value.reasons)
    assert batch.snapshot() == before

    single = Batch(tmp_path / "single")
    evidence, _, _ = single.evidence("Keep weekly status notes to five lines.")
    sha = single.write_packet([single.item(1, "Keep weekly status notes short.", [evidence])])
    single.write_card(["Card 9001 YES"], sha)
    assert single.run()["status"] == "applied"
    assert single.heads()[single.node("Principle", sha, 1)]["provenance_status"] == "ratified"


def test_all_yes_approves_every_item_and_conflicting_answers_refuse(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(["9001 all yes", "9001.2 no"], sha)
    with pytest.raises(Refusal) as refused:
        batch.run()
    assert any("item 2 has conflicting answers" in reason for reason in refused.value.reasons)
    assert not batch.out.exists()

    batch.write_card(["9001 all yes", "9001.2 yes"], sha)
    result = batch.run()
    assert result["status"] == "applied" and result["readback"][0]["bound_claims"] == 3
    pinned = [pin["node_id"] for pin in json.loads(batch.manifest().read_text())["bindings"]]
    assert pinned == [batch.node("Principle", sha, 1), batch.node("Rule", sha, 2), batch.node("Belief", sha, 3)]


def test_refuses_a_card_that_does_not_show_this_packet(tmp_path):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(["9001 all yes"], sha, objects=["Some other packet"], what="Ratify something.")
    with pytest.raises(Refusal) as refused:
        batch.run()
    assert any("does not name this packet's sha256" in reason for reason in refused.value.reasons)

    batch.write_card(["9001 all yes"], sha, killed_by="owner", withdrawn_at="2026-10-28T13:00:00")
    with pytest.raises(Refusal) as refused:
        batch.run()
    assert "card 9001 was withdrawn by its owner" in refused.value.reasons
    assert not batch.out.exists()


def test_second_packet_appends_to_the_owner_manifest(tmp_path):
    batch = Batch(tmp_path)
    first_evidence, _, _ = batch.evidence("Keep weekly status notes to five lines.")
    first = batch.write_packet([batch.item(1, "Keep weekly status notes short.", [first_evidence])])
    batch.write_card(["9001 yes"], first)
    assert batch.run()["status"] == "applied"

    second_evidence, _, _ = batch.evidence("Lead every summary with the decision.")
    second = batch.write_packet([batch.item(1, "Summaries open with the decision.", [second_evidence])],
                                card=9002)
    batch.write_card(["9002.1 yes"], second, card=9002)
    result = batch.run()

    assert result["status"] == "applied" and result["readback"][0]["bound_claims"] == 2
    pinned = [pin["node_id"] for pin in json.loads(batch.manifest().read_text())["bindings"]]
    assert pinned == [batch.node("Principle", first, 1), batch.node("Principle", second, 1)]


def test_pattern_items_are_pinned_and_domain_items_are_not(tmp_path):
    batch = Batch(tmp_path)
    one, case_one, _ = batch.evidence("Short notes landed better in review.")
    two, case_two, _ = batch.evidence("The short note was read; the long one was not.")
    sha = batch.write_packet([
        batch.item(1, "Short notes get read.", [one, two], node_type="Pattern", case_ids=[case_one, case_two]),
        batch.item(2, "Status reporting for internal reviews.", [one], node_type="Domain",
                   domain_id="status-reporting", public_label="Status reporting"),
    ])
    batch.write_card(["9001 all yes"], sha)

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
    batch.write_card(["9001 all yes"], sha)
    before = batch.snapshot()

    plan = batch.run(dry_run=True)

    assert plan["status"] == "dry_run" and [entry["steps"] for entry in plan["pending"]] == [["append", "ratify"]] * 3
    assert batch.snapshot() == before and not batch.out.exists()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for synced in ("Documents/ratify", "Desktop", "Library/Mobile Documents/ratify", "Dropbox/ratify"):
        with pytest.raises(Refusal):
            batch.run(out_root=tmp_path / "home" / synced)
        assert not (tmp_path / "home").exists()
    with pytest.raises(Refusal):
        batch.run(out_root=Path("relative/ratify"))


def test_cli_requires_an_explicit_config_and_reports_refusals(tmp_path, capsys, monkeypatch):
    batch = Batch(tmp_path)
    sha = _three_item_batch(batch)
    batch.write_card(["9001.1 yes"], sha)
    arguments = ["ratify", "apply", "--packet", str(batch.packet), "--out-root", str(batch.out),
                 "--reader", str(batch.reader)]
    monkeypatch.setenv("AGENTS_CARD_STORE", str(batch.cards))

    assert main(arguments) == 2
    assert "requires --config" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--config", str(batch.config), *arguments]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "refused" and "card 9001 has no recorded answer for item(s) 2, 3" in refused["reasons"]

    batch.write_card(["9001.1 yes", "9001.2 yes", "9001.3 yes"], sha)
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
    ("9001.4 yes", ([], None, "answer '9001.4 yes' names item 4, but the packet has 3 item(s)")),
])
def test_answer_grammar(line, expected):
    assert parse_answer(line, CARD, 3) == expected


@pytest.mark.skipif(not os.environ.get("IMPRINT_RATIFY_READER"),
                    reason="set IMPRINT_RATIFY_READER to the native judgment reader to run")
def test_readback_through_the_native_reader(tmp_path):
    batch = Batch(tmp_path, reader=Path(os.environ["IMPRINT_RATIFY_READER"]))
    sha = _three_item_batch(batch)
    batch.write_card(["9001.1 yes", "9001.2 no", "9001.3 fix tighten the wording"], sha)

    result = batch.run()

    assert result["status"] == "applied", result
    assert result["readback"] == [{"manifest": str(batch.manifest()), "problems": [],
                                   "bound_claims": 1, "unbound_claims": 0}]
    assert batch.run()["status"] == "already_applied"
