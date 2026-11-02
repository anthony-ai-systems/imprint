"""The scope carrier: the row, its stamp, and what keeps it. Every id here is synthetic."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from imprint.capture import CapturePipeline
from imprint.capture.schema import build_capture_envelope, new_urn
from imprint.cli import main
from imprint.compiler import compile_spools, write_envelope
from imprint.errors import ValidationError
from imprint.ingest import IngestCandidate, IngestService, import_legacy_principles
from imprint.purge import hard_purge
from imprint.retrieve import RetrievalEngine, StoreRetrievalSource, retrieve_payload
from imprint.retrieve.engine import eligible
from imprint.retrieve.models import RecordScope
from imprint.store import ImprintStore
from imprint.store.scope import SCOPE_EXTENSION, write_scope_row


ROOT = Path(__file__).resolve().parents[2]
ACTOR = "synthetic-operator"
VALID_FROM = "2026-07-14T12:00:00Z"


def extension(**payload) -> dict:
    return {SCOPE_EXTENSION: {"schema_version": "1.0.0", "payload": {
        "scope_kind": "client", "client_id": "example-client", "resolved_by": "thread_folder", **payload,
    }}}


def stamped(envelope: dict, **payload) -> dict:
    value = deepcopy(envelope)
    value["extensions"] = extension(**payload)
    return value


def row_of(store: ImprintStore, node_id: str) -> dict | None:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM record_scope WHERE node_id=?", (node_id,)).fetchone()
    return dict(row) if row else None


def rows_of(store: ImprintStore) -> list[dict]:
    with store.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM record_scope ORDER BY node_id")]


def new_store(tmp_path: Path) -> ImprintStore:
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    return store


def rule(store: ImprintStore, envelope: dict, text: str = "Say which source failed.", **fields) -> str:
    """A candidate Rule on the capture's evidence, which the caller may ratify."""
    return store.append_derived_node(
        node_type="Rule", payload={"statement": text, **fields.pop("payload", {})},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[envelope["evidence"][0]["evidence_id"]], operator_id=envelope["operator_id"],
        valid_from=VALID_FROM, proposed_by="test", **fields,
    )


def central(store: ImprintStore, envelope: dict, text: str = "Name the failed source.") -> str:
    """No build writes a central row yet, so a test puts one on a ratified record."""
    node_id = rule(store, envelope, text)
    event_id = store.ratify_node(node_id, ratifier=ACTOR)
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        write_scope_row(conn, {
            "node_id": node_id, "scope_kind": "central", "client_id": None, "project_id": None,
            "delivery": "general", "delivery_rank": 1, "speaker": None, "endorser": None,
            "overrides_json": "[]", "resolved_by": "ratified", "event_id": event_id,
            "stamped_at": VALID_FROM,
        })
    return node_id


def configured(tmp_path: Path, operator_id: str) -> tuple[Path, ImprintStore]:
    data = tmp_path / "data"
    root = data / "test-operator"
    root.mkdir(parents=True)
    (root / "identity.json").write_text(json.dumps({"identity_schema_version": "1.0.0", "operator_id": operator_id}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "operator_slug": "test-operator", "data_root": str(data), "compiler": True, "node_id": "workstation-a",
    }))
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    return config, store


# The extension and the row in apply_capture --------------------------------


def test_an_old_spool_compiles_with_no_quarantine_and_stays_legacy(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    write_envelope(tmp_path, capture_envelope)
    counts = compile_spools(tmp_path, store, compiler_authorized=True)
    assert counts == {"captured": 1, "duplicate": 0, "quarantined": 0}
    assert rows_of(store) == []
    assert "scope" not in store.node_history(capture_envelope["verdict"]["verdict_id"])


def test_a_stamp_lands_on_the_verdict_in_the_capture_event(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(
        capture_envelope, project_id="example-project", speaker="a-team-member", endorser="the-operator",
        delivery="general", delivery_rank=3,
    )
    assert store.apply_capture(envelope) == "captured"
    verdict_id = envelope["verdict"]["verdict_id"]
    row = rows_of(store)[0]
    assert len(rows_of(store)) == 1 and row["node_id"] == verdict_id
    assert row["event_id"] == envelope["input_event_id"]
    assert {key: row[key] for key in (
        "scope_kind", "client_id", "project_id", "delivery", "delivery_rank", "speaker", "endorser",
        "overrides_json", "resolved_by",
    )} == {
        "scope_kind": "client", "client_id": "example-client", "project_id": "example-project",
        "delivery": "general", "delivery_rank": 3, "speaker": "a-team-member",
        "endorser": "the-operator", "overrides_json": "[]", "resolved_by": "thread_folder",
    }
    with store.connect() as conn:
        event = json.loads(conn.execute(
            "SELECT payload_json FROM events WHERE event_id=?", (envelope["input_event_id"],),
        ).fetchone()[0])
    assert event["extensions"][SCOPE_EXTENSION]["payload"]["client_id"] == "example-client"


def test_imprint_capture_carries_a_stamp_through_the_spool(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    event = tmp_path / "event.json"
    event.write_text(json.dumps(stamped(capture_envelope)))
    assert main(["--config", str(config), "capture", "--event", str(event)]) == 0
    assert main(["--config", str(config), "compile", "--once"]) == 0
    capsys.readouterr()
    assert [row["client_id"] for row in rows_of(store)] == ["example-client"]


@pytest.mark.parametrize("with_extension", [True, False])
def test_the_capture_pipeline_passes_extensions_through(tmp_path, capture_envelope, with_extension):
    store = new_store(tmp_path)

    class Spool:
        def persist(self, envelope):
            return store.apply_capture(envelope)

    fields = {"extensions": extension()} if with_extension else {}
    result = CapturePipeline(Spool()).capture(
        operator_text="Do not do that, say which source failed.", case_description="A draft",
        operator_id=capture_envelope["operator_id"], session_id=new_urn("session"), node_id="workstation-a",
        capture_mechanism="explicit_cli", captured_by="imprint-test", **fields,
    )
    assert result.persisted
    assert len(rows_of(store)) == (1 if with_extension else 0)


@pytest.mark.parametrize("payload", [
    {"scope_kind": "central", "client_id": None},
    {"speaker": "a-team-member"},
    {"overrides": ["urn:imprint:rule:00000000-0000-4000-8000-000000000000"]},
    {"scope_kind": "client", "client_id": None},
    {"scope_kind": "system"},
    {"scope_kind": "unresolved", "client_id": None, "delivery": "general"},
    {"scope_kind": "system", "client_id": None, "delivery": "general"},
    {"scope_kind": "held", "client_id": None, "delivery": "specific"},
    {"scope_kind": "shared", "client_id": None},
    {"resolved_by": None},
    {"delivery_rank": True},
    {"delivery_rank": "1"},
    {"candidate_client_ids": ["example-client"]},
    {"scope_kind": "held", "client_id": None, "candidate_client_ids": ["b-client", "a-client"]},
    {"scope_kind": "held", "client_id": None, "candidate_client_ids": ["a-client", "a-client"]},
    {"client_id": "  "},
], ids=repr)
def test_a_bad_stamp_is_refused_and_writes_nothing(tmp_path, capture_envelope, payload):
    store = new_store(tmp_path)
    with pytest.raises(ValidationError):
        store.apply_capture(stamped(capture_envelope, **payload))
    assert store.current_nodes() == [] and rows_of(store) == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumed_inputs").fetchone()[0] == 0


def test_central_is_refused_on_capture_by_name(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    with pytest.raises(ValidationError, match="only at ratification"):
        store.apply_capture(stamped(capture_envelope, scope_kind="central", client_id=None))


def test_a_stamp_of_an_unknown_version_is_refused(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope)
    envelope["extensions"][SCOPE_EXTENSION]["schema_version"] = "9.0.0"
    with pytest.raises(ValidationError, match="version"):
        store.apply_capture(envelope)


def test_a_refused_stamp_quarantines_its_own_spool_and_nothing_else(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    other = build_capture_envelope(
        operator_id=capture_envelope["operator_id"], session_id=new_urn("session"), node_id="workstation-a",
        case_description="Another draft", raw_operator_text="Keep the second line.", call_type="accept",
        capture_mechanism="explicit_cli", captured_by="imprint-test",
    )
    write_envelope(tmp_path, stamped(capture_envelope, scope_kind="central", client_id=None))
    write_envelope(tmp_path, other)
    counts = compile_spools(tmp_path, store, compiler_authorized=True)
    assert counts == {"captured": 1, "duplicate": 0, "quarantined": 1}
    assert rows_of(store) == []


def test_recompiling_a_retained_spool_adds_no_row_and_moves_none(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope)
    write_envelope(tmp_path, envelope)
    compile_spools(tmp_path, store, compiler_authorized=True)
    before = rows_of(store)
    assert compile_spools(tmp_path, store, compiler_authorized=True) == {
        "captured": 0, "duplicate": 1, "quarantined": 0,
    }
    assert rows_of(store) == before


# imprint scope set ---------------------------------------------------------


def test_scope_set_appends_an_event_updates_the_row_and_reads_back(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    envelope = stamped(
        capture_envelope, scope_kind="held", client_id=None, resolved_by="conflict",
        candidate_client_ids=["a-client", "b-client"],
    )
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    versions = len(store.node_history(verdict_id)["versions"])

    assert main(["--config", str(config), "scope", "set", verdict_id, "--kind", "client", "--client", "c-client",
                 "--project", "a-project", "--delivery", "general", "--rank", "4", "--by", ACTOR]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "scope_changed"
    row = row_of(store, verdict_id)
    assert (row["scope_kind"], row["client_id"], row["project_id"], row["delivery"], row["delivery_rank"]) == (
        "client", "c-client", "a-project", "general", 4)
    assert (row["resolved_by"], row["event_id"]) == ("explicit", result["event_id"])

    assert main(["--config", str(config), "history", verdict_id]) == 0
    history = json.loads(capsys.readouterr().out)["history"]
    assert len(history["versions"]) == versions
    assert history["scope"]["client_id"] == "c-client" and history["scope"]["overrides"] == []
    change = history["dispositions"][-1]
    assert change["event_type"] == "scope_changed" and change["event_id"] == result["event_id"]
    assert change["payload"]["prior_scope"]["scope_kind"] == "held"
    assert change["payload"]["scope"]["client_id"] == "c-client"
    assert change["payload"]["actor_id"] == ACTOR


def test_scope_set_moves_only_what_it_names(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    envelope = stamped(capture_envelope, project_id="a-project", delivery="specific", delivery_rank=2,
                       speaker="a-team-member", endorser="the-operator")
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    assert main(["--config", str(config), "scope", "set", verdict_id, "--delivery", "general", "--by", ACTOR]) == 0
    row = row_of(store, verdict_id)
    assert (row["delivery"], row["delivery_rank"], row["project_id"], row["client_id"]) == (
        "general", 2, "a-project", "example-client")
    assert (row["speaker"], row["endorser"]) == ("a-team-member", "the-operator")
    assert main(["--config", str(config), "scope", "set", verdict_id, "--clear", "delivery", "--clear", "rank",
                 "--clear", "project", "--by", ACTOR]) == 0
    row = row_of(store, verdict_id)
    assert (row["delivery"], row["delivery_rank"], row["project_id"]) == (None, None, None)
    assert main(["--config", str(config), "scope", "set", verdict_id, "--kind", "system", "--by", ACTOR]) == 0
    assert row_of(store, verdict_id)["client_id"] is None
    capsys.readouterr()


def test_scope_set_records_overrides_only_on_its_own_event(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    target = central(store, envelope)
    event_id = store.set_record_scope(verdict_id, actor_id=ACTOR, changes={"overrides": [target]})
    assert json.loads(row_of(store, verdict_id)["overrides_json"]) == [target]
    with store.connect() as conn:
        naming = [row[0] for row in conn.execute(
            "SELECT event_type FROM events WHERE event_id<>? AND payload_json LIKE ? AND event_type<>'ratified'"
            " AND event_type NOT LIKE 'inferred%'",
            (event_id, f'%"overrides"%{target}%'),
        )]
    assert naming == []
    store.set_record_scope(verdict_id, actor_id=ACTOR, changes={"overrides": []})
    assert row_of(store, verdict_id)["overrides_json"] == "[]"


@pytest.mark.parametrize("changes, message", [
    ({}, "must name"),
    ({"scope_kind": "central"}, "only at ratification"),
    ({"scope_kind": "shared"}, "unknown scope kind"),
    ({"scope_kind": "client"}, "client id"),
    ({"scope_kind": "system", "client_id": "a-client"}, "client id"),
    ({"scope_kind": "system", "delivery": "general"}, "delivery tag"),
    ({"scope_kind": "unresolved", "delivery": "specific"}, "delivery tag"),
    ({"scope_kind": "held", "delivery": "general"}, "delivery tag"),
    ({"scope_kind": "client", "client_id": "a-client", "delivery": "broad"}, "unknown delivery"),
    ({"scope_kind": "client", "client_id": "a-client", "delivery_rank": True}, "rank"),
    ({"scope_kind": "client", "client_id": "a-client", "speaker": "someone"}, "must name"),
    ({"scope_kind": "client", "client_id": "a-client", "overrides": ["urn:imprint:rule:nope"]}, "current central"),
], ids=repr)
def test_scope_set_refusals_change_nothing(tmp_path, capture_envelope, changes, message):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    node_id = rule(store, capture_envelope)
    with pytest.raises(ValidationError, match=message):
        store.set_record_scope(node_id, actor_id=ACTOR, changes=changes)
    assert row_of(store, node_id) is None
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='scope_changed'").fetchone()[0] == 0


def test_scope_set_needs_a_kind_where_no_row_exists(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    with pytest.raises(ValidationError, match="needs a kind"):
        store.set_record_scope(rule(store, capture_envelope), actor_id=ACTOR, changes={"delivery": "general"})


def test_a_central_row_keeps_its_kind_but_can_be_retagged(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    target = central(store, capture_envelope)
    for kind in ("client", "system", "held", "unresolved"):
        with pytest.raises(ValidationError, match="keeps its kind"):
            store.set_record_scope(target, actor_id=ACTOR, changes={"scope_kind": kind, "client_id": None})
    store.set_record_scope(target, actor_id=ACTOR, changes={"delivery": "specific"})
    row = row_of(store, target)
    assert (row["scope_kind"], row["delivery"]) == ("central", "specific")


def test_scope_set_refuses_an_override_that_is_not_a_current_central_record(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    plain = rule(store, envelope, "Not central.")
    closed = central(store, envelope, "Closed central.")
    store.tombstone_node(closed, reason="Withdrawn.")
    for override in (plain, closed, verdict_id):
        with pytest.raises(ValidationError, match="current central"):
            store.set_record_scope(verdict_id, actor_id=ACTOR, changes={"overrides": [override]})


def test_scope_set_refuses_a_closed_record_and_an_unknown_one(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    store.tombstone_node(verdict_id, reason="Withdrawn.")
    for node_id in (verdict_id, "urn:imprint:rule:00000000-0000-4000-8000-000000000000"):
        with pytest.raises(ValidationError, match="missing or not current"):
            store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "system"})


def test_scope_set_refuses_a_record_that_is_never_retrieved(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    for node_id in (capture_envelope["case"]["case_id"], capture_envelope["evidence"][0]["evidence_id"]):
        with pytest.raises(ValidationError, match="not retrievable"):
            store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "system"})


def test_scope_set_keeps_a_tag_to_the_three_taggable_types(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    evidence_id = capture_envelope["evidence"][0]["evidence_id"]
    belief = store.append_derived_node(
        node_type="Belief", payload={"statement": "A belief."}, provenance_status="inferred",
        authority_tier="inferred_candidate", evidence_ids=[evidence_id],
        operator_id=capture_envelope["operator_id"], valid_from=VALID_FROM, proposed_by="test",
    )
    with pytest.raises(ValidationError, match="Verdict, Principle or Rule"):
        store.set_record_scope(belief, actor_id=ACTOR, changes={
            "scope_kind": "client", "client_id": "a-client", "delivery": "general",
        })


def test_scope_set_keeps_a_client_off_a_record_with_a_domain_id(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    node_id = rule(store, capture_envelope, payload={"domain_id": "research"})
    with pytest.raises(ValidationError, match="domain_id"):
        store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "client", "client_id": "a-client"})
    store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "system"})


def test_scope_set_cli_says_what_it_refuses(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    store.apply_capture(capture_envelope)
    verdict_id = capture_envelope["verdict"]["verdict_id"]
    assert main(["--config", str(config), "scope", "set", verdict_id, "--kind", "central", "--by", ACTOR]) == 2
    assert "only at ratification" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--config", str(config), "scope", "set", verdict_id, "--kind", "system",
                 "--delivery", "specific", "--clear", "delivery", "--by", ACTOR]) == 2
    assert "both set and clear" in json.loads(capsys.readouterr().out)["error"]
    assert rows_of(store) == []


# Purge ---------------------------------------------------------------------


def test_a_held_capture_reads_its_candidates_back_and_purges_clean(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    envelope = stamped(
        capture_envelope, scope_kind="held", client_id=None, resolved_by="conflict",
        candidate_client_ids=["a-client", "b-client"],
    )
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    row = row_of(store, verdict_id)
    assert (row["scope_kind"], row["client_id"], row["resolved_by"]) == ("held", None, "conflict")

    assert main(["--config", str(config), "history", verdict_id]) == 0
    history = json.loads(capsys.readouterr().out)["history"]
    payload = history["versions"][0]["event_payload"]["extensions"][SCOPE_EXTENSION]["payload"]
    assert payload["candidate_client_ids"] == ["a-client", "b-client"]
    assert history["scope"]["scope_kind"] == "held"

    root = store.path.parent
    result = hard_purge(store, root, verdict_id, confirmation=verdict_id, sentinel="b-client")
    assert result["status"] == "purged"
    assert rows_of(store) == []
    assert b"b-client" not in store.path.read_bytes()


def test_a_purge_takes_the_row_with_the_node(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope, project_id="a-distinctive-project")
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    result = hard_purge(store, tmp_path, verdict_id, confirmation=verdict_id, sentinel="a-distinctive-project")
    assert result["status"] == "purged"
    assert rows_of(store) == []


def test_a_purge_with_a_central_id_in_an_override_list_leaves_no_residue(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    keeper = central(store, envelope, "A central rule that stays.")
    doomed = central(store, envelope, "A central rule that goes.")
    store.set_record_scope(verdict_id, actor_id=ACTOR, changes={"overrides": [doomed, keeper]})
    result = hard_purge(store, tmp_path, doomed, confirmation=doomed)
    assert result["status"] == "purged", result
    assert json.loads(row_of(store, verdict_id)["overrides_json"]) == [keeper]
    assert row_of(store, doomed) is None
    assert doomed.encode() not in store.path.read_bytes()


# Table 7: a new version keeps the row --------------------------------------


def _load_review_helpers():
    spec = importlib.util.spec_from_file_location(
        "semantic_review_helpers", ROOT / "tests" / "unit" / "test_semantic_review.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _typed_proposal(tmp_path, envelope):
    store, operator_id, _, contract = _load_review_helpers()._setup(tmp_path, envelope)
    node_id = contract["node_id"]
    store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "system"})
    return store, operator_id, contract, node_id


@pytest.mark.parametrize("change", ["add_reason", "reinforce"])
def test_later_verdict_evidence_keeps_the_row(tmp_path, capture_envelope, change):
    store = new_store(tmp_path)
    capture_envelope["verdict"]["reason"], capture_envelope["verdict"]["reason_status"] = None, "absent"
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    before = row_of(store, verdict_id)
    if change == "add_reason":
        store.add_reason(verdict_id, reason="It changes the conclusion.", actor_id=ACTOR)
    else:
        store.reinforce_verdict(verdict_id, evidence_text="Said again in review.", actor_id=ACTOR)
    assert len(store.node_history(verdict_id)["versions"]) == 2
    assert row_of(store, verdict_id) == before


def test_ratifying_in_place_keeps_the_row(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    node_id = rule(store, capture_envelope)
    store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "system"})
    before = row_of(store, node_id)
    store.ratify_node(node_id, ratifier=ACTOR)
    assert row_of(store, node_id) == before


def test_a_ratify_stamp_never_replaces_a_row_already_there(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    node_id = rule(store, capture_envelope)
    store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "client", "client_id": "a-client"})
    before = row_of(store, node_id)
    store.ratify_node(node_id, ratifier=ACTOR, scope={"scope_kind": "unresolved", "resolved_by": "test"})
    assert row_of(store, node_id) == before


def test_rejecting_keeps_the_row_on_the_closed_node(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    node_id = rule(store, capture_envelope)
    store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "held"})
    before = row_of(store, node_id)
    store.reject_node(node_id, rejector=ACTOR, reason="Not a rule.")
    assert row_of(store, node_id) == before


def test_deferring_keeps_the_row(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    node_id = rule(store, capture_envelope)
    store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "system"})
    before = row_of(store, node_id)
    store.defer_node(node_id, reviewer=ACTOR, reason="Needs a second case.")
    assert row_of(store, node_id) == before


def test_contesting_keeps_the_row(tmp_path, capture_envelope):
    store, operator_id, _, node_id = _typed_proposal(tmp_path, capture_envelope)
    before = row_of(store, node_id)
    store.contest_typed_node(node_id, contestor=operator_id, reason="This is not how it reads.")
    assert row_of(store, node_id) == before


def test_correcting_keeps_the_row(tmp_path, capture_envelope):
    store, operator_id, proposal, node_id = _typed_proposal(tmp_path, capture_envelope)
    corrected = deepcopy(proposal)
    corrected["payload"]["statement"] = "Completion pressure requires a scope check before reframing."
    corrected["payload"]["review_state"] = "corrected"
    corrected["payload"]["provenance"] = {
        "status": "ratified", "actor_class": "operator", "actor_id": operator_id,
        "model_id": None, "prompt_id": None,
    }
    corrected["provenance"] = {
        "status": "ratified", "authority_tier": "ratified_knowledge", "actor_class": "operator",
        "actor_id": operator_id, "mechanism": "explicit_operator_correction",
        "evidence_ids": proposal["provenance"]["evidence_ids"], "model": None, "ratifier_id": operator_id,
    }
    before = row_of(store, node_id)
    store.correct_typed_node(node_id, corrected_contract=corrected, corrector=operator_id, reason="Sharper.")
    assert row_of(store, node_id) == before


def test_a_tombstone_leaves_the_row_with_the_closed_node(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope)
    store.apply_capture(envelope)
    verdict_id = envelope["verdict"]["verdict_id"]
    before = row_of(store, verdict_id)
    store.tombstone_node(verdict_id, reason="Withdrawn.")
    assert row_of(store, verdict_id) == before
    assert verdict_id not in {node["node_id"] for node in store.current_nodes()}


def _supersede(store, envelope, source, target):
    return store.add_transition(
        "supersedes", source, target, reason="Replaced.",
        evidence_ids=[envelope["evidence"][0]["evidence_id"]], actor_id=ACTOR,
    )


def test_a_successor_keeps_its_own_row(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    old, new = rule(store, capture_envelope, "Old."), rule(store, capture_envelope, "New.")
    store.set_record_scope(old, actor_id=ACTOR, changes={"scope_kind": "client", "client_id": "a-client"})
    store.set_record_scope(new, actor_id=ACTOR, changes={"scope_kind": "system"})
    before = row_of(store, new)
    _supersede(store, capture_envelope, new, old)
    assert row_of(store, new) == before


def test_a_successor_with_no_row_takes_the_targets_in_the_same_event(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    old, new = rule(store, capture_envelope, "Old."), rule(store, capture_envelope, "New.")
    store.set_record_scope(old, actor_id=ACTOR, changes={
        "scope_kind": "client", "client_id": "a-client", "project_id": "a-project", "delivery": "general",
    })
    target = row_of(store, old)
    edge_id = _supersede(store, capture_envelope, new, old)
    with store.connect() as conn:
        event_id = conn.execute("SELECT created_event_id FROM edges WHERE edge_id=?", (edge_id,)).fetchone()[0]
    copied = row_of(store, new)
    assert copied["event_id"] == event_id
    keep = ("scope_kind", "client_id", "project_id", "delivery", "delivery_rank", "overrides_json", "resolved_by")
    assert {key: copied[key] for key in keep} == {key: target[key] for key in keep}
    assert row_of(store, old) == target


def test_contradiction_touches_no_row(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    old, new = rule(store, capture_envelope, "Old."), rule(store, capture_envelope, "New.")
    store.set_record_scope(old, actor_id=ACTOR, changes={"scope_kind": "client", "client_id": "a-client"})
    store.add_transition(
        "contradicts", new, old, reason="Disagrees.",
        evidence_ids=[capture_envelope["evidence"][0]["evidence_id"]], actor_id=ACTOR,
    )
    assert row_of(store, new) is None


def test_a_client_record_cannot_supersede_a_central_one(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    target = central(store, capture_envelope)
    new = rule(store, capture_envelope, "Client wording.")
    store.set_record_scope(new, actor_id=ACTOR, changes={"scope_kind": "client", "client_id": "a-client"})
    with pytest.raises(ValidationError, match="cannot supersede a central"):
        _supersede(store, capture_envelope, new, target)
    assert target in {node["node_id"] for node in store.current_nodes()}


def test_a_central_row_is_never_copied_onto_a_successor(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    target = central(store, capture_envelope)
    new = rule(store, capture_envelope, "Unscoped wording.")
    with pytest.raises(ValidationError, match="cannot supersede a central"):
        _supersede(store, capture_envelope, new, target)
    assert row_of(store, new) is None
    assert target in {node["node_id"] for node in store.current_nodes()}


def test_a_successor_with_its_own_non_client_row_may_close_a_central_record(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    target = central(store, capture_envelope)
    new = rule(store, capture_envelope, "System wording.")
    store.set_record_scope(new, actor_id=ACTOR, changes={"scope_kind": "system"})
    _supersede(store, capture_envelope, new, target)
    assert row_of(store, new)["scope_kind"] == "system"


def test_supersession_keeps_a_domain_id_off_a_copied_client_row(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    old = rule(store, capture_envelope, "Old.")
    new = rule(store, capture_envelope, "New.", payload={"domain_id": "research"})
    store.set_record_scope(old, actor_id=ACTOR, changes={"scope_kind": "client", "client_id": "a-client"})
    with pytest.raises(ValidationError, match="domain_id"):
        _supersede(store, capture_envelope, new, old)


def test_a_derived_node_cannot_put_a_client_on_a_domain_id(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    with pytest.raises(ValidationError, match="domain_id"):
        rule(store, capture_envelope, payload={"domain_id": "research"},
             scope={"scope_kind": "client", "client_id": "a-client", "resolved_by": "explicit"})


# The read side -------------------------------------------------------------


def test_the_row_joins_onto_its_record_and_only_its_record(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    envelope = stamped(capture_envelope, project_id="a-project", delivery="general", delivery_rank=5,
                       speaker="a-team-member", endorser="the-operator")
    store.apply_capture(envelope)
    plain = rule(store, envelope, "A rule with no row.")
    target = central(store, envelope)
    store.set_record_scope(envelope["verdict"]["verdict_id"], actor_id=ACTOR, changes={"overrides": [target]})
    records = {item.record_id: item for item in StoreRetrievalSource(store).retrieval_candidates("x")}
    assert records[envelope["verdict"]["verdict_id"]].scope == RecordScope(
        scope_kind="client", resolved_by="explicit", client_id="example-client", project_id="a-project",
        delivery="general", delivery_rank=5, speaker="a-team-member", endorser="the-operator",
        overrides=(target,),
    )
    assert records[plain].scope is None
    assert records[target].scope.scope_kind == "central"
    assert len({records[plain], records[target]}) == 2


def test_rows_change_no_delivered_byte(tmp_path, capture_envelope):
    """A stamp moves the snapshot id. It moves nothing a thread is handed."""
    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    verdict_id = capture_envelope["verdict"]["verdict_id"]
    node_id = rule(store, capture_envelope)
    store.ratify_node(node_id, ratifier=ACTOR)

    def delivered() -> dict:
        return retrieve_payload(store, root=tmp_path, session_id="s", prompt="source", refresh=True)

    before = delivered()
    store.set_record_scope(verdict_id, actor_id=ACTOR, changes={"scope_kind": "held"})
    store.set_record_scope(node_id, actor_id=ACTOR, changes={
        "scope_kind": "client", "client_id": "a-client", "delivery": "specific", "delivery_rank": 1,
    })
    after = delivered()
    assert after["snapshot_id"] != before["snapshot_id"]
    assert {**after, "snapshot_id": None} == {**before, "snapshot_id": None}


def test_the_snapshot_id_holds_on_an_empty_table_and_moves_at_the_first_stamp(tmp_path, capture_envelope):
    from imprint.retrieve import _snapshot_id

    store = new_store(tmp_path)
    store.apply_capture(capture_envelope)
    empty = _snapshot_id(store)
    assert set(store.snapshot()) == {"store_schema_version", "ontology_schema_version", "nodes", "edges"}
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM record_scope").fetchone()[0] == 0
    store.set_record_scope(capture_envelope["verdict"]["verdict_id"], actor_id=ACTOR, changes={"scope_kind": "system"})
    stamped_id = _snapshot_id(store)
    assert stamped_id != empty
    assert [row["scope_kind"] for row in store.snapshot()["record_scope"]] == ["system"]
    store.set_record_scope(capture_envelope["verdict"]["verdict_id"], actor_id=ACTOR, changes={"scope_kind": "held"})
    assert _snapshot_id(store) != stamped_id


def _older_store(tmp_path: Path, envelope: dict) -> ImprintStore:
    """A store as a build from before the table left it."""
    store = new_store(tmp_path)
    store.apply_capture(envelope)
    with store.connect() as conn:
        conn.execute("DROP TABLE record_scope")
    return store


def _table_exists(store: ImprintStore) -> bool:
    with store.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='record_scope'",
        ).fetchone() is not None


def test_a_missing_table_reads_as_empty_in_the_store(tmp_path, capture_envelope):
    store = _older_store(tmp_path, capture_envelope)
    assert set(store.snapshot()) == {"store_schema_version", "ontology_schema_version", "nodes", "edges"}
    assert "scope" not in store.node_history(capture_envelope["verdict"]["verdict_id"])
    assert not _table_exists(store)


def test_a_missing_table_reads_as_empty_for_retrieval_callers(tmp_path, capture_envelope):
    """The bridge and ``capture_context.py`` reach the store this way, with no initialize."""
    store = _older_store(tmp_path, capture_envelope)
    records = list(StoreRetrievalSource(store).retrieval_candidates("x"))
    assert records and all(item.scope is None for item in records)
    assert all(eligible(item, None) for item in records)
    result = RetrievalEngine(StoreRetrievalSource(store)).retrieve(snapshot_id="x", query="source")
    assert result.selected_ids
    assert not _table_exists(store)


def test_a_missing_table_reads_as_empty_for_health(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    store.apply_capture(capture_envelope)
    with store.connect() as conn:
        conn.execute("DROP TABLE record_scope")
    main(["--config", str(config), "health"])
    assert json.loads(capsys.readouterr().out)["status"] in {"healthy", "degraded"}
    assert not _table_exists(store)


def test_a_missing_table_reads_as_empty_for_a_transition_and_a_purge(tmp_path, capture_envelope):
    store = _older_store(tmp_path, capture_envelope)
    old, new = rule(store, capture_envelope, "Old."), rule(store, capture_envelope, "New.")
    _supersede(store, capture_envelope, new, old)
    result = hard_purge(store, tmp_path, new, confirmation=new)
    assert result["status"] == "purged"


def test_the_first_initialize_adds_the_table_to_an_older_store(tmp_path, capture_envelope):
    store = _older_store(tmp_path, capture_envelope)
    store.initialize()
    assert _table_exists(store)
    assert rows_of(store) == []


def test_store_and_record_versions_stay_put():
    from imprint.constants import RECORD_SCHEMA_VERSION, STORE_SCHEMA_VERSION

    assert (STORE_SCHEMA_VERSION, RECORD_SCHEMA_VERSION) == ("3.0.0", "3.0.0")


# Table 6: the other paths that make a record -------------------------------


def _principle_contract(operator_id: str, evidence_id: str, node_type: str = "Principle") -> dict:
    from imprint.ratify import _contract

    item = {"node_type": node_type, "statement": "A synthetic principle.", "evidence_ids": [evidence_id]}
    if node_type == "Domain":
        item.update(domain_id="synthetic-domain", public_label="Synthetic")
    return _contract(item, new_urn(node_type.lower()), operator_id, "synthetic-model")


def _write_input(tmp_path: Path, contract: dict) -> str:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    return str(path)


def test_an_ontology_flag_stamps_the_new_node_and_stays_out_of_its_event(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    store.apply_capture(capture_envelope)
    contract = _principle_contract(capture_envelope["operator_id"], capture_envelope["evidence"][0]["evidence_id"])
    assert main(["--config", str(config), "ontology", "add-node", "--input", _write_input(tmp_path, contract),
                 "--valid-from", VALID_FROM, "--scope-kind", "client", "--scope-client", "a-client",
                 "--scope-project", "a-project"]) == 0
    node_id = json.loads(capsys.readouterr().out)["node_id"]
    row = row_of(store, node_id)
    assert (row["scope_kind"], row["client_id"], row["project_id"], row["resolved_by"]) == (
        "client", "a-client", "a-project", "explicit")
    with store.connect() as conn:
        event = conn.execute("SELECT payload_json FROM events WHERE event_id=?", (row["event_id"],)).fetchone()[0]
    assert "a-client" not in event


def test_an_ontology_add_with_no_flag_writes_no_row(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    store.apply_capture(capture_envelope)
    contract = _principle_contract(capture_envelope["operator_id"], capture_envelope["evidence"][0]["evidence_id"])
    assert main(["--config", str(config), "ontology", "add-node", "--input", _write_input(tmp_path, contract),
                 "--valid-from", VALID_FROM]) == 0
    capsys.readouterr()
    assert rows_of(store) == []


def test_central_is_refused_on_an_ontology_flag(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    store.apply_capture(capture_envelope)
    contract = _principle_contract(capture_envelope["operator_id"], capture_envelope["evidence"][0]["evidence_id"])
    assert main(["--config", str(config), "ontology", "add-node", "--input", _write_input(tmp_path, contract),
                 "--valid-from", VALID_FROM, "--scope-kind", "central"]) == 2
    assert "only at ratification" in json.loads(capsys.readouterr().out)["error"]
    assert contract["node_id"] not in {node["node_id"] for node in store.current_nodes()}


def test_ontology_scope_flags_need_a_kind_and_a_retrievable_type(tmp_path, capsys, capture_envelope):
    config, store = configured(tmp_path, capture_envelope["operator_id"])
    store.apply_capture(capture_envelope)
    evidence_id = capture_envelope["evidence"][0]["evidence_id"]
    contract = _principle_contract(capture_envelope["operator_id"], evidence_id)
    assert main(["--config", str(config), "ontology", "add-node", "--input", _write_input(tmp_path, contract),
                 "--valid-from", VALID_FROM, "--scope-client", "a-client"]) == 2
    assert "need --scope-kind" in json.loads(capsys.readouterr().out)["error"]
    domain = _principle_contract(capture_envelope["operator_id"], evidence_id, "Domain")
    assert main(["--config", str(config), "ontology", "add-node", "--input", _write_input(tmp_path, domain),
                 "--valid-from", VALID_FROM, "--scope-kind", "system"]) == 2
    assert "not retrievable" in json.loads(capsys.readouterr().out)["error"]


def _specimen(tenant_scope: str, *, flagged: bool = False, locator: str = "1") -> IngestCandidate:
    return IngestCandidate(
        source_kind="session_transcript", source_locator=f"synthetic://transcript#L{locator}",
        content=f"[preference] Keep the second line.\nWHY: synthetic {locator}",
        metadata={"session_id": "synthetic", "project_dir": "/synthetic/project", "tenant_scope": tenant_scope},
        extensions={"taste.v1": {"kind": "preference", "scope": tenant_scope, "scope_ambiguous": flagged}},
    )


@pytest.mark.parametrize("tenant_scope, flagged, kind, resolved_by", [
    ("global", False, "unresolved", "ingest_unflagged"),
    ("entity:example-entity", False, "unresolved", "ingest_unflagged"),
    ("global", True, "held", "ingest_ambiguous"),
    ("entity:example-entity", True, "held", "ingest_ambiguous"),
])
def test_a_kept_specimen_stamps_from_the_distillers_fields(tmp_path, capture_envelope, tenant_scope, flagged, kind, resolved_by):
    store = new_store(tmp_path)
    service = IngestService(store, capture_envelope["operator_id"])
    item_id = service.scan([_specimen(tenant_scope, flagged=flagged)])[0]["item_id"]
    service.keep(item_id, why="A real preference.", stamp_scope=True)
    kept = store.current_nodes(["IngestedItem"])[0]
    row = row_of(store, kept["node_id"])
    assert (row["scope_kind"], row["client_id"], row["resolved_by"]) == (kind, None, resolved_by)
    assert kept["payload"]["metadata"]["tenant_scope"] == tenant_scope
    assert kept["payload"]["metadata"]["project_dir"] == "/synthetic/project"


def test_a_keep_writes_no_row_until_stamping_exists(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    service = IngestService(store, capture_envelope["operator_id"])
    item_id = service.scan([_specimen("global", flagged=True)])[0]["item_id"]
    service.keep(item_id, why="A real preference.")
    assert rows_of(store) == []


def test_a_kill_writes_no_row_even_with_stamping(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    service = IngestService(store, capture_envelope["operator_id"])
    item_id = service.scan([_specimen("global")])[0]["item_id"]
    service.kill(item_id, why="Not useful.")
    assert rows_of(store) == []


def test_a_held_kept_specimen_is_released_by_scope_set(tmp_path, capture_envelope):
    store = new_store(tmp_path)
    service = IngestService(store, capture_envelope["operator_id"])
    item_id = service.scan([_specimen("entity:example-entity", flagged=True)])[0]["item_id"]
    service.keep(item_id, why="A real preference.", stamp_scope=True)
    node_id = store.current_nodes(["IngestedItem"])[0]["node_id"]
    store.set_record_scope(node_id, actor_id=ACTOR, changes={"scope_kind": "client", "client_id": "a-client"})
    assert row_of(store, node_id)["scope_kind"] == "client"


def test_a_legacy_import_stamps_unresolved_unless_told_otherwise(tmp_path):
    store = new_store(tmp_path)
    operator_id = new_urn("operator")
    plain, explicit = import_legacy_principles(
        store, [{"text": "An old principle."}], operator_id=operator_id, source_version="1",
        source_locator="synthetic://legacy/1",
    ) + import_legacy_principles(
        store, [{"text": "Another old principle."}], operator_id=operator_id, source_version="1",
        source_locator="synthetic://legacy/2",
        scope={"scope_kind": "client", "client_id": "a-client", "resolved_by": "explicit"},
    )
    assert (row_of(store, plain)["scope_kind"], row_of(store, plain)["resolved_by"]) == ("unresolved", "legacy_import")
    assert (row_of(store, explicit)["scope_kind"], row_of(store, explicit)["client_id"]) == ("client", "a-client")
    with pytest.raises(ValidationError, match="only at ratification"):
        import_legacy_principles(
            store, [{"text": "A third."}], operator_id=operator_id, source_version="1",
            source_locator="synthetic://legacy/3", scope={"scope_kind": "central", "resolved_by": "x"},
        )
