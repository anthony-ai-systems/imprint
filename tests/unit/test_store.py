from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from imprint.compiler import (
    compile_spools, compiler_lock_state, prune_acknowledged_spools,
    recover_stale_compiler_lock, write_envelope,
)
from imprint.compiler.spool import LOCK_STALE_SECONDS
from imprint.constants import ONTOLOGY_SCHEMA_VERSION
from imprint.errors import ConflictError, SafetyError, ValidationError
from imprint.capture.schema import validate_capture_envelope
from imprint.capture.schema import build_capture_envelope, new_urn
from imprint.ontology.schema import canonical_bytes, make_urn, payload_sha256
from imprint.portability import export_jsonld, import_jsonld
from imprint.portability.jsonld import semantic_digest
from imprint.projections import jsonld_document, markdown_document
from imprint.store import ImprintStore


def test_capture_persists_raw_graph(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    assert store.apply_capture(capture_envelope) == "captured"
    types = {node["node_type"] for node in store.current_nodes()}
    assert types == {"Case", "Verdict", "Call", "Evidence"}
    edges = {edge["edge_type"] for edge in store.current_edges()}
    assert edges == {"verdict_about_case", "made_call", "supported_by"}
    assert all(node["provenance_status"] == "captured" for node in store.current_nodes())
    assert all(edge["provenance_status"] == "captured" for edge in store.current_edges())
    required = {
        "provenance_schema_version", "status", "authority_tier", "actor_class",
        "actor_id", "mechanism", "software", "model", "prompt_recipe",
        "proposal_id", "ratifier", "event_id", "relation",
    }
    assert all(set(item["provenance"]) == required for item in [*store.current_nodes(), *store.current_edges()])
    assert store.integrity_check() == "ok"


def test_capture_fails_closed_on_wrong_operator_or_producer_node(tmp_path, capture_envelope):
    store = ImprintStore(
        tmp_path / "imprint.db",
        expected_operator_id=capture_envelope["operator_id"],
        expected_node_id=capture_envelope["node_id"],
    )
    store.initialize()
    wrong_operator = json.loads(json.dumps(capture_envelope))
    wrong_operator["operator_id"] = new_urn("operator")
    wrong_operator["provenance"]["actor_id"] = wrong_operator["operator_id"]
    with pytest.raises(ValidationError, match="operator"):
        store.apply_capture(wrong_operator)
    wrong_node = json.loads(json.dumps(capture_envelope))
    wrong_node["node_id"] = "foreign-producer"
    with pytest.raises(ValidationError, match="producer node"):
        store.apply_capture(wrong_node)
    assert store.current_nodes() == []


def test_semantic_relation_fails_before_lookup_for_foreign_configured_operator(tmp_path):
    configured = make_urn("operator")
    foreign = make_urn("operator")
    store = ImprintStore(tmp_path / "imprint.db", expected_operator_id=configured)
    store.initialize()
    relation = {
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "relation_id": make_urn("relation"), "relation_type": "inferred_from",
        "source_id": make_urn("principle"), "source_type": "Principle",
        "target_id": make_urn("verdict"), "target_type": "Verdict",
        "operator_id": foreign, "evidence_mode": "inferred",
        "why": "A foreign configured identity cannot write this relation.",
        "provenance": {
            "status": "inferred", "authority_tier": "inferred_candidate",
            "actor_class": "model", "actor_id": make_urn("model"),
            "mechanism": "identity_boundary_test", "evidence_ids": [make_urn("evidence")],
            "model": "synthetic-model", "ratifier_id": None,
        },
    }
    with pytest.raises(ValidationError, match="configured identity"):
        store.append_semantic_relation(relation, valid_from="2026-07-14T12:00:00Z")
    assert store.current_edges() == []


def test_null_reason_is_first_class(tmp_path, capture_envelope):
    capture_envelope["verdict"]["reason"] = None
    capture_envelope["verdict"]["reason_status"] = "pending"
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    verdict = store.current_nodes(["Verdict"])[0]
    assert verdict["payload"]["reason"] is None
    assert verdict["payload"]["reason_status"] == "pending"


def test_alternatives_survive_projection(tmp_path, capture_envelope):
    chosen = make_urn("alternative")
    rejected = make_urn("alternative")
    capture_envelope["alternatives"] = [
        {"alternative_id": chosen, "description": "Report the failed source", "disposition": "chosen"},
        {"alternative_id": rejected, "description": "Hide it in a positive summary", "disposition": "rejected"},
    ]
    capture_envelope["verdict"]["chosen_alternative_ids"] = [chosen]
    capture_envelope["verdict"]["rejected_alternative_ids"] = [rejected]
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    snapshot = store.snapshot()
    graph = jsonld_document(snapshot)["@graph"]
    assert any(item.get("@type") == "imprint:Alternative" and item["payload"]["description"] == "Report the failed source" for item in graph)
    assert {edge["edge_type"] for edge in snapshot["edges"]} >= {"chose_alternative", "rejected_alternative"}


def test_same_event_is_idempotent_and_collision_fails(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    assert store.apply_capture(capture_envelope) == "captured"
    assert store.apply_capture(capture_envelope) == "duplicate"
    capture_envelope["case"]["description"] = "different bytes"
    with pytest.raises(ConflictError):
        store.apply_capture(capture_envelope)


def test_spool_is_immutable_and_foreign_files_unchanged(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    path = write_envelope(root, capture_envelope)
    before = path.read_bytes()
    store = ImprintStore(root / "imprint.db")
    counts = compile_spools(root, store, compiler_authorized=True)
    assert counts == {"captured": 1, "duplicate": 0, "quarantined": 0}
    assert path.read_bytes() == before
    counts = compile_spools(root, store, compiler_authorized=True)
    assert counts == {"captured": 0, "duplicate": 1, "quarantined": 0}
    assert path.read_bytes() == before


def test_acknowledged_spool_retention_is_time_relative_and_producer_scoped(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    path = write_envelope(root, capture_envelope)
    store = ImprintStore(root / "imprint.db")
    assert compile_spools(root, store, compiler_authorized=True)["captured"] == 1
    ack = next((root / "runtime" / "acknowledgements" / capture_envelope["node_id"]).glob("*.json"))
    value = json.loads(ack.read_text())
    value["acknowledged_at"] = "2026-07-01T00:00:00Z"
    ack.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    early = prune_acknowledged_spools(
        root, source_node_id=capture_envelope["node_id"], retention_days=14,
        now=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    assert early == {"deleted": 0, "retained": 1, "invalid": 0}
    assert path.exists()
    due = prune_acknowledged_spools(
        root, source_node_id=capture_envelope["node_id"], retention_days=14,
        now=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert due == {"deleted": 1, "retained": 0, "invalid": 0}
    assert not path.exists() and ack.exists()


def test_noncompiler_refuses_mutation(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    write_envelope(root, capture_envelope)
    with pytest.raises(SafetyError):
        compile_spools(root, ImprintStore(root / "imprint.db"), compiler_authorized=False)


def test_second_compiler_refuses_existing_writer_lock(tmp_path, capture_envelope, monkeypatch):
    import imprint.compiler.spool as spool_module

    root = tmp_path / "operator"
    write_envelope(root, capture_envelope)
    (root / "compiler.lock").mkdir()
    monkeypatch.setattr(spool_module, "LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(spool_module, "LOCK_RETRY_INTERVAL_SECONDS", 0.01)
    with pytest.raises(SafetyError, match="second writer"):
        compile_spools(root, ImprintStore(root / "imprint.db"), compiler_authorized=True)


def test_compiler_retries_until_contended_lock_is_released(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    write_envelope(root, capture_envelope)
    lock = root / "compiler.lock"
    lock.mkdir()

    def release_lock():
        time.sleep(0.3)
        lock.rmdir()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        assert compile_spools(
            root, ImprintStore(root / "imprint.db"), compiler_authorized=True,
        )["captured"] == 1
    finally:
        releaser.join(timeout=2)
    assert not releaser.is_alive()


def test_compiler_lock_state_and_exact_nonce_stale_recovery(tmp_path, monkeypatch):
    root = tmp_path / "operator"
    lock = root / "compiler.lock"
    lock.mkdir(parents=True)
    assert compiler_lock_state(root)["state"] == "invalid"
    assert compiler_lock_state(root)["stale"] is False
    old_mtime = time.time() - LOCK_STALE_SECONDS - 2
    os.utime(lock, (old_mtime, old_mtime))
    with pytest.raises(SafetyError, match="RECOVER-INVALID-LOCK"):
        recover_stale_compiler_lock(root, confirmation="anything")

    nonce = "a" * 32
    (lock / "owner.json").write_text(json.dumps({
        "lock_schema_version": "1.0.0", "nonce": nonce, "pid": 1,
        "host": "test-host", "created_at": "2000-01-01T00:00:00Z",
        "heartbeat_at": "2000-01-01T00:00:00Z",
    }))
    state = compiler_lock_state(root)
    assert state["state"] == "held" and state["stale"] is True
    with pytest.raises(SafetyError, match="exact owner nonce"):
        recover_stale_compiler_lock(root, confirmation="b" * 32)
    assert recover_stale_compiler_lock(root, confirmation=nonce) == {
        "status": "recovered", "nonce": nonce,
    }
    assert compiler_lock_state(root) == {"state": "absent", "stale": False}


def test_malformed_spool_creates_content_free_quarantine_receipt(tmp_path):
    root = tmp_path / "operator"
    spool = root / "spool" / "node-a"
    spool.mkdir(parents=True)
    source = spool / "malformed.json"
    source.write_text('{"private":"sentinel-secret"')
    before = source.read_bytes()
    counts = compile_spools(root, ImprintStore(root / "imprint.db"), compiler_authorized=True)
    assert counts == {"captured": 0, "duplicate": 0, "quarantined": 1}
    assert source.read_bytes() == before
    receipts = list((root / "quarantine").glob("*.json"))
    assert len(receipts) == 1
    receipt_text = receipts[0].read_text()
    assert "sentinel-secret" not in receipt_text
    assert json.loads(receipt_text)["content_included"] is False


def _second_capture(capture_envelope, *, captured_at, description, text, reason):
    return build_capture_envelope(
        operator_id=capture_envelope["operator_id"],
        session_id=new_urn("session"),
        node_id=capture_envelope["node_id"],
        case_description=description,
        raw_operator_text=text,
        call_type="correct",
        capture_mechanism="explicit_cli",
        captured_by="imprint-test",
        reason=reason,
        captured_at=captured_at,
    )


def test_conflicting_case_id_quarantines_one_input_without_halting_compile(tmp_path, capture_envelope):
    root = tmp_path / "operator"
    conflicting = _second_capture(
        capture_envelope,
        captured_at="2026-07-14T18:00:01Z",
        description="A distinct case that reuses an already committed case_id",
        text="Name the source that failed because a silent gap changes the conclusion.",
        reason="A silent gap changes the conclusion.",
    )
    conflicting["case"]["case_id"] = capture_envelope["case"]["case_id"]
    later = _second_capture(
        capture_envelope,
        captured_at="2026-07-14T18:00:02Z",
        description="An unrelated case spooled behind the conflicting one",
        text="State the confidence interval because a point estimate overstates certainty.",
        reason="A point estimate overstates certainty.",
    )
    for envelope in (capture_envelope, conflicting, later):
        write_envelope(root, envelope)
    assert len({e["input_event_id"] for e in (capture_envelope, conflicting, later)}) == 3

    store = ImprintStore(root / "imprint.db")
    counts = compile_spools(root, store, compiler_authorized=True)
    assert counts == {"captured": 2, "duplicate": 0, "quarantined": 1}

    receipts = [json.loads(path.read_text()) for path in (root / "quarantine").glob("*.json")]
    assert len(receipts) == 1
    assert receipts[0]["error_type"] == "ConflictError"
    assert receipts[0]["content_included"] is False

    node_ids = {node["node_id"] for node in store.current_nodes()}
    assert later["case"]["case_id"] in node_ids
    # The rejected envelope must leave nothing behind: its own verdict and call
    # ids are unique to it, so their absence proves the write was rolled back.
    assert conflicting["verdict"]["verdict_id"] not in node_ids
    assert conflicting["verdict"]["call"]["call_id"] not in node_ids


def test_unknown_top_level_and_authority_escalation_fail(capture_envelope):
    capture_envelope["surprise"] = True
    with pytest.raises(ValidationError):
        validate_capture_envelope(capture_envelope)
    capture_envelope.pop("surprise")
    capture_envelope["provenance"]["status"] = "ratified"
    with pytest.raises(ValidationError):
        validate_capture_envelope(capture_envelope)


def test_projection_is_deterministic(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    one = markdown_document(store.snapshot())
    two = markdown_document(store.snapshot())
    assert one == two
    assert "[captured" in one
    assert capture_envelope["verdict"]["raw_operator_text"] in one
    projected = jsonld_document(store.snapshot())
    assert json.dumps(projected, sort_keys=True) == json.dumps(jsonld_document(store.snapshot()), sort_keys=True)
    assert all(isinstance(item["provenance"], dict) for item in projected["@graph"])
    assert all(item["provenanceStatus"] == item["provenance"]["status"] for item in projected["@graph"])


def test_inferred_pattern_requires_multiple_evidence_records(tmp_path):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    with pytest.raises(ValidationError, match="at least two cases"):
        store.append_derived_node(
            node_type="Pattern",
            payload={"statement": "Prefer explicit failure reports"},
            provenance_status="inferred",
            authority_tier="inferred_pattern",
            evidence_ids=[make_urn("evidence")],
            operator_id=make_urn("operator"),
            valid_from="2026-07-14T12:00:00Z",
            proposed_by="derive-agent",
        )


def test_derived_node_stays_non_authoritative_until_explicit_ratification(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    evidence_ids = [item["node_id"] for item in store.current_nodes(["Evidence"])]
    node_id = store.append_derived_node(
        node_type="Principle",
        payload={"statement": "Report every material source failure"},
        provenance_status="inferred",
        authority_tier="inferred_candidate",
        evidence_ids=evidence_ids,
        operator_id=capture_envelope["operator_id"],
        valid_from="2026-07-14T12:00:00Z",
        proposed_by="derive-agent",
    )
    before = store.current_nodes(["Principle"])[0]
    assert before["node_id"] == node_id
    assert before["provenance_status"] == "inferred"
    deferred = store.defer_node(
        node_id,
        reviewer="operator",
        reason="Need another live example",
        revisit_after="2026-08-01T12:00:00Z",
    )
    assert deferred.startswith("urn:imprint:event:")
    assert store.current_nodes(["Principle"])[0]["provenance_status"] == "inferred"
    assert store.node_history(node_id)["dispositions"][-1]["event_type"] == "deferred"
    event_id = store.ratify_node(node_id, ratifier="operator", note="Confirmed explicitly")
    after = store.current_nodes(["Principle"])[0]
    assert after["provenance_status"] == "ratified"
    assert after["authority_tier"] == "ratified_knowledge"
    assert event_id.startswith("urn:imprint:event:")
    with store.connect() as conn:
        versions = conn.execute(
            "SELECT provenance_status,system_to FROM node_versions WHERE node_id=? ORDER BY system_from",
            (node_id,),
        ).fetchall()
    assert [row["provenance_status"] for row in versions] == ["inferred", "ratified"]
    assert versions[0]["system_to"] is not None
    assert versions[1]["system_to"] is None


def test_strict_semantic_writer_preserves_typed_contract_and_legacy_path_cannot_write_new_types(
    tmp_path, capture_envelope,
):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    evidence_id = store.current_nodes(["Evidence"])[0]["node_id"]
    node_id = make_urn("principle")
    model_id = make_urn("model")
    contract = {
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "node_id": node_id,
        "node_type": "Principle",
        "operator_id": capture_envelope["operator_id"],
        "payload": {"statement": "Report material source failures explicitly."},
        "provenance": {
            "status": "inferred",
            "authority_tier": "inferred_candidate",
            "actor_class": "model",
            "actor_id": model_id,
            "mechanism": "typed_ontology_proposal",
            "evidence_ids": [evidence_id],
            "model": "synthetic-test-model",
            "ratifier_id": None,
        },
    }
    assert store.append_semantic_node(contract, valid_from="2026-07-14T12:00:00Z") == node_id
    saved = store.current_nodes(["Principle"])[0]
    assert saved["payload"] == contract["payload"]
    assert saved["provenance_status"] == "inferred"
    assert saved["authority_tier"] == "inferred_candidate"

    verdict_id = store.current_nodes(["Verdict"])[0]["node_id"]
    relation_id = make_urn("relation")
    relation = {
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "relation_id": relation_id,
        "relation_type": "inferred_from",
        "source_id": node_id,
        "source_type": "Principle",
        "target_id": verdict_id,
        "target_type": "Verdict",
        "operator_id": capture_envelope["operator_id"],
        "evidence_mode": "inferred",
        "why": "The proposed principle was inferred from this witnessed verdict.",
        "provenance": contract["provenance"],
    }
    assert store.append_semantic_relation(
        relation, valid_from="2026-07-14T12:00:00Z",
    ) == relation_id
    saved_edge = next(edge for edge in store.current_edges() if edge["edge_id"] == relation_id)
    assert saved_edge["edge_type"] == "inferred_from"
    assert saved_edge["provenance_status"] == "inferred"

    with pytest.raises(ValidationError, match="unsupported derived ontology"):
        store.append_derived_node(
            node_type="SelfModelAssertion",
            payload={}, provenance_status="inferred", authority_tier="inferred_candidate",
            evidence_ids=[evidence_id], operator_id=capture_envelope["operator_id"],
            valid_from="2026-07-14T12:00:00Z", proposed_by="legacy-path",
        )


def test_semantic_observation_requires_matching_durable_consent(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    operator_id = capture_envelope["operator_id"]
    evidence_id = store.current_nodes(["Evidence"])[0]["node_id"]
    grant_id = make_urn("consentgrant")
    grant_payload = {
        "ontology_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "operator_id": operator_id,
        "source_class": "transcript",
        "purposes": ["behavioral_observation"],
        "sensitivity": "sensitive",
        "allowed_operations": ["store"],
        "retention": {"mode": "until_revoked", "days": None, "delete_on_revoke": True},
        "effective_from": "2026-07-01T00:00:00Z",
        "effective_to": None,
        "granted_by": operator_id,
        "granted_at": "2026-07-01T00:00:00Z",
        "revoked_at": None,
        "revocation_reason": None,
        "extensions": {},
    }
    captured_provenance = {
        "status": "captured", "authority_tier": "captured_judgment",
        "actor_class": "operator", "actor_id": operator_id,
        "mechanism": "explicit_consent", "evidence_ids": [],
        "model": None, "ratifier_id": None,
    }
    store.append_semantic_node({
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "node_id": grant_id, "node_type": "ConsentGrant", "operator_id": operator_id,
        "payload": grant_payload, "provenance": captured_provenance,
    }, valid_from="2026-07-01T00:00:00Z")

    confidence = {
        "score": 0.8, "assessor_id": "synthetic-observer", "method": "model_estimate",
        "basis_evidence_ids": [evidence_id], "assessed_at": "2026-07-14T12:00:00Z",
        "calibration_trial_id": None, "uncertainty_note": "Single observation.",
    }
    observation_payload = {
        "ontology_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "operator_id": operator_id, "source_class": "transcript",
        "observation_kind": "behavior", "subject_id": operator_id,
        "description": "Explicitly reports failed sources.",
        "observed_at": "2026-07-14T12:00:00Z",
        "window_start": "2026-07-14T11:00:00Z", "window_end": "2026-07-14T13:00:00Z",
        "evidence_ids": [evidence_id], "confidence": confidence,
        "consent_grant_id": grant_id, "attributes": {}, "extensions": {},
    }
    observed_provenance = {
        "status": "extracted", "authority_tier": "observed_candidate",
        "actor_class": "software", "actor_id": make_urn("software"),
        "mechanism": "typed_observation", "evidence_ids": [evidence_id],
        "model": None, "ratifier_id": None,
    }
    observation_id = make_urn("observation")
    store.append_semantic_node({
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "node_id": observation_id, "node_type": "Observation", "operator_id": operator_id,
        "payload": observation_payload, "provenance": observed_provenance,
    }, valid_from="2026-07-14T12:00:00Z")
    assert store.current_nodes(["Observation"])[0]["node_id"] == observation_id

    portable = export_jsonld(store)
    unauthorized = json.loads(json.dumps(portable))
    grant_version = next(
        row for row in unauthorized["imprint:ledger"]["node_versions"]
        if row["node_id"] == grant_id
    )
    changed_grant = json.loads(grant_version["payload_json"])
    changed_grant["purposes"] = ["self_modeling"]
    grant_version["payload_json"] = canonical_bytes(changed_grant).decode()
    grant_version["payload_sha256"] = payload_sha256(changed_grant)
    grant_graph = next(item for item in unauthorized["@graph"] if item["@id"] == grant_version["version_id"])
    grant_graph["imprint:payload"] = changed_grant
    grant_graph["imprint:payloadSha256"] = grant_version["payload_sha256"]
    grant_node = next(row for row in unauthorized["imprint:ledger"]["nodes"] if row["node_id"] == grant_id)
    grant_event = next(
        row for row in unauthorized["imprint:ledger"]["events"]
        if row["event_id"] == grant_node["created_event_id"]
    )
    grant_event_payload = json.loads(grant_event["payload_json"])
    grant_event_payload["payload"] = changed_grant
    grant_event["payload_json"] = canonical_bytes(grant_event_payload).decode()
    grant_event["payload_sha256"] = payload_sha256(grant_event_payload)
    unauthorized["imprint:semanticSha256"] = semantic_digest(unauthorized)
    unauthorized_target = ImprintStore(tmp_path / "unauthorized-import.db")
    with pytest.raises(ValidationError, match="does not authorize imported"):
        import_jsonld(unauthorized_target, unauthorized)
    assert not unauthorized_target.path.exists()

    event_mismatch = json.loads(json.dumps(portable))
    observation_version = next(
        row for row in event_mismatch["imprint:ledger"]["node_versions"]
        if row["node_id"] == observation_id
    )
    changed_observation = json.loads(observation_version["payload_json"])
    changed_observation["description"] = "Altered without changing its creation event."
    observation_version["payload_json"] = canonical_bytes(changed_observation).decode()
    observation_version["payload_sha256"] = payload_sha256(changed_observation)
    observation_graph = next(
        item for item in event_mismatch["@graph"] if item["@id"] == observation_version["version_id"]
    )
    observation_graph["imprint:payload"] = changed_observation
    observation_graph["imprint:payloadSha256"] = observation_version["payload_sha256"]
    event_mismatch["imprint:semanticSha256"] = semantic_digest(event_mismatch)
    with pytest.raises(ValidationError, match="creation event does not match"):
        import_jsonld(ImprintStore(tmp_path / "event-mismatch.db"), event_mismatch)

    denied = json.loads(json.dumps(observation_payload))
    denied["source_class"] = "screenpipe"
    with pytest.raises(ValidationError, match="does not authorize"):
        store.append_semantic_node({
            "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
            "node_id": make_urn("observation"), "node_type": "Observation", "operator_id": operator_id,
            "payload": denied, "provenance": observed_provenance,
        }, valid_from="2026-07-14T12:00:00Z")

    store.revoke_consent(
        grant_id, operator_id=operator_id, reason="Stop transcript observation",
        revoked_at="2026-07-14T12:30:00Z",
    )
    revoked = store.current_nodes(["ConsentGrant"])[0]
    assert revoked["payload"]["revoked_at"] == "2026-07-14T12:30:00Z"
    with pytest.raises(ValidationError, match="does not authorize"):
        store.append_semantic_node({
            "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
            "node_id": make_urn("observation"), "node_type": "Observation", "operator_id": operator_id,
            "payload": observation_payload, "provenance": observed_provenance,
        }, valid_from="2026-07-14T13:00:00Z")


def test_self_model_review_state_survives_defer_then_ratification(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    operator_id = capture_envelope["operator_id"]
    evidence_id = store.current_nodes(["Evidence"])[0]["node_id"]
    model_id = make_urn("model")
    with store.connect() as conn:
        evidence_version_id = conn.execute(
            "SELECT version_id FROM node_versions WHERE node_id=? AND system_to IS NULL",
            (evidence_id,),
        ).fetchone()[0]
    trace_id = make_urn("derivationtrace")
    store.append_semantic_node({
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "node_id": trace_id, "node_type": "DerivationTrace", "operator_id": operator_id,
        "payload": {
            "ontology_schema_version": ONTOLOGY_SCHEMA_VERSION,
            "operator_id": operator_id, "element_version_id": evidence_version_id,
            "source_phase": "approved_import", "derived_from_rule": "synthetic-test-v1",
            "computed_at": "2026-07-14T12:00:00Z", "input_ids": [evidence_id],
            "input_snapshot_sha256": "a" * 64, "model_id": "synthetic-model",
            "prompt_id": "synthetic-prompt-v1", "extensions": {},
        },
        "provenance": {
            "status": "inferred", "authority_tier": "inferred_candidate",
            "actor_class": "model", "actor_id": model_id,
            "mechanism": "synthetic_derivation", "evidence_ids": [evidence_id],
            "model": "synthetic-model", "ratifier_id": None,
        },
    }, valid_from="2026-07-14T12:00:00Z")
    payload = {
        "ontology_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "operator_id": operator_id, "function_class": "Psyche",
        "dimension": "blind_spot", "subtype": "psyche_element",
        "statement": "Completion pressure can trigger unnecessary reframing.",
        "polarity": "constraint", "scope": "public release work", "source_phase": "approved_import",
        "derivation_trace_id": trace_id, "evidence_ids": [evidence_id],
        "confidence": {
            "score": 0.7, "assessor_id": "synthetic-model", "method": "model_estimate",
            "basis_evidence_ids": [evidence_id], "assessed_at": "2026-07-14T12:00:00Z",
            "calibration_trial_id": None, "uncertainty_note": "Requires operator review.",
        },
        "freshness": {
            "valid_from": "2026-07-14T12:00:00Z", "valid_to": None,
            "last_reviewed_at": None, "revalidate_after": "2026-08-14T12:00:00Z",
            "evidence_window_start": "2026-07-01T00:00:00Z",
            "evidence_window_end": "2026-07-14T12:00:00Z", "status": "current",
        },
        "review_state": "proposed", "structure": {},
        "provenance": {
            "status": "inferred", "actor_class": "model", "actor_id": model_id,
            "model_id": "synthetic-model", "prompt_id": "synthetic-prompt-v1",
        },
        "extensions": {},
    }
    node_id = make_urn("selfmodelassertion")
    store.append_semantic_node({
        "record_schema_version": ONTOLOGY_SCHEMA_VERSION,
        "node_id": node_id, "node_type": "SelfModelAssertion", "operator_id": operator_id,
        "payload": payload,
        "provenance": {
            "status": "inferred", "authority_tier": "inferred_candidate",
            "actor_class": "model", "actor_id": model_id,
            "mechanism": "zmos_proposal_import", "evidence_ids": [evidence_id],
            "model": "synthetic-model", "ratifier_id": None,
        },
    }, valid_from="2026-07-14T12:00:00Z")

    store.defer_node(node_id, reviewer=operator_id, reason="Need another example")
    deferred = store.current_nodes(["SelfModelAssertion"])[0]
    assert deferred["payload"]["review_state"] == "deferred"
    assert deferred["provenance_status"] == "inferred"

    store.ratify_node(node_id, ratifier=operator_id, note="Confirmed with scope intact")
    confirmed = store.current_nodes(["SelfModelAssertion"])[0]
    assert confirmed["payload"]["review_state"] == "confirmed"
    assert confirmed["payload"]["provenance"]["status"] == "ratified"
    assert confirmed["provenance_status"] == "ratified"


def test_ratification_requires_nonblank_operator_identity(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    evidence_id = store.current_nodes(["Evidence"])[0]["node_id"]
    node_id = store.append_derived_node(
        node_type="Rule", payload={"statement": "Synthetic"},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[evidence_id], operator_id=capture_envelope["operator_id"],
        valid_from="2026-07-14T12:00:00Z", proposed_by="test",
    )
    with pytest.raises(ValidationError, match="ratifier identity"):
        store.ratify_node(node_id, ratifier="  ")
    assert store.current_nodes(["Rule"])[0]["provenance_status"] == "inferred"


def test_tombstone_preserves_history_and_removes_current_authority(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    verdict_id = capture_envelope["verdict"]["verdict_id"]
    event_id = store.tombstone_node(verdict_id, reason="Operator reversed this judgment")
    assert event_id.startswith("urn:imprint:event:")
    assert verdict_id not in {node["node_id"] for node in store.current_nodes()}
    assert all(
        verdict_id not in {edge["source_id"], edge["target_id"]}
        for edge in store.current_edges()
    )
    with store.connect() as conn:
        historical = conn.execute(
            "SELECT COUNT(*) FROM node_versions WHERE node_id=? AND system_to IS NOT NULL",
            (verdict_id,),
        ).fetchone()[0]
        reversal = conn.execute(
            "SELECT event_type,payload_json FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
    assert historical == 1
    assert reversal["event_type"] == "tombstoned"
    assert json.loads(reversal["payload_json"])["reason"] == "Operator reversed this judgment"


def test_ratification_cannot_launder_captured_or_already_ratified_nodes(tmp_path, capture_envelope):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    store.apply_capture(capture_envelope)
    with pytest.raises(ValidationError, match="only inferred or extracted"):
        store.ratify_node(capture_envelope["verdict"]["verdict_id"], ratifier="operator")


def test_canonical_capture_with_alternatives_compiles_end_to_end(tmp_path):
    envelope = build_capture_envelope(
        operator_id=new_urn("operator"),
        session_id=new_urn("session"),
        node_id="node-integration",
        case_description="Choosing how to report a failed source",
        raw_operator_text="Use the explicit failure report, not the softened summary.",
        call_type="prefer",
        capture_mechanism="explicit_cli",
        captured_by="integration-test",
        chosen_alternatives=["Explicitly identify the failed source"],
        rejected_alternatives=["Soften the result and omit the source failure"],
    )
    root = tmp_path / "operator"
    write_envelope(root, envelope)
    store = ImprintStore(root / "imprint.db")
    assert compile_spools(root, store, compiler_authorized=True) == {
        "captured": 1,
        "duplicate": 0,
        "quarantined": 0,
    }
    alternatives = store.current_nodes(["Alternative"])
    assert {item["payload"]["disposition"] for item in alternatives} == {"chosen", "rejected"}
    assert {item["edge_type"] for item in store.current_edges()} >= {
        "chose_alternative",
        "rejected_alternative",
    }


def _plant_wal_residue(tmp_path, target_db, marker="committed_before_kill"):
    """Plant crash residue at target_db: a committed row that lives only in the WAL
    sidecar, with no live connection -- exactly the state a SIGKILL mid-hook leaves.

    Fully in-process and cross-platform: it snapshots the on-disk db+wal+shm bytes
    after a commit but before any checkpoint (autocheckpoint disabled, no close),
    then writes them over the target, reproducing an orphaned WAL byte-for-byte.
    """
    base = Path(tmp_path) / "_residue_base.db"
    ImprintStore(base).initialize()
    conn = sqlite3.connect(str(base))
    try:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO meta(key,value) VALUES('recover_probe',?)", (marker,))
        conn.commit()
        snapshot = {
            suffix: Path(str(base) + suffix).read_bytes()
            for suffix in ("", "-wal", "-shm")
            if Path(str(base) + suffix).exists()
        }
    finally:
        conn.close()
    for suffix, data in snapshot.items():
        Path(str(target_db) + suffix).write_bytes(data)


def test_recover_replays_committed_wal_residue_without_data_loss(tmp_path):
    target = tmp_path / "imprint.db"
    _plant_wal_residue(tmp_path, target)
    store = ImprintStore(target)

    # The committed marker is not yet in the main database file; it lives in the WAL.
    read_only = sqlite3.connect(
        f"{target.resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    try:
        assert read_only.execute(
            "SELECT value FROM meta WHERE key='recover_probe'"
        ).fetchone() is None
    finally:
        read_only.close()

    result = store.recover()
    assert result["status"] == "recovered"
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()

    # The committed capture survived: it was replayed into the main database.
    with store.connect() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='recover_probe'"
        ).fetchone()
    assert row[0] == "committed_before_kill"
    assert store.integrity_check() == "ok"


def _fast_settle(monkeypatch, attempts=2, interval=0.01):
    monkeypatch.setattr(ImprintStore, "_SIDECAR_SETTLE_ATTEMPTS", attempts)
    monkeypatch.setattr(ImprintStore, "_SIDECAR_SETTLE_INTERVAL_SECONDS", interval)


def test_connect_auto_recovers_stale_wal_residue(tmp_path):
    """Crash residue with no live writer heals on the next ordinary open.

    Before this, one SIGKILLed hook bricked every later hook until a manual
    ``store recover``; the guard could not tell residue from a live writer.
    """
    target = tmp_path / "imprint.db"
    _plant_wal_residue(tmp_path, target)
    store = ImprintStore(target)

    with store.connect() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='recover_probe'"
        ).fetchone()
    assert row[0] == "committed_before_kill"
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()
    assert store.integrity_check() == "ok"
    # The drain leaves WAL mode to shed the sidecars; it must have restored it.
    check = sqlite3.connect(str(target))
    try:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        check.close()


def test_connect_still_refuses_while_live_writer_holds_the_store(tmp_path, monkeypatch):
    _fast_settle(monkeypatch)
    target = tmp_path / "imprint.db"
    store = ImprintStore(target)
    store.initialize()

    live = sqlite3.connect(str(target))
    try:
        live.execute("INSERT INTO meta(key,value) VALUES('live_probe','1')")
        live.commit()
        # Hold an open read snapshot so the WAL cannot be reset by another connection.
        live.execute("BEGIN")
        live.execute("SELECT * FROM meta").fetchall()
        assert Path(str(target) + "-wal").exists()
        with pytest.raises(ValidationError, match="WAL/SHM"):
            with store.connect():
                pass
    finally:
        live.close()


def test_connect_refuses_while_an_idle_connection_holds_the_store(tmp_path, monkeypatch):
    """An idle-open connection must refuse, not drain.

    A checkpoint sails past a connection that holds no read mark, so a
    checkpoint-based drain would delete the sidecars out from under it and
    split later writers onto fresh WAL/SHM files. Leaving WAL mode is refused
    by SQLite while any other connection is open, idle or not.
    """
    _fast_settle(monkeypatch)
    target = tmp_path / "imprint.db"
    store = ImprintStore(target)
    store.initialize()

    idle = sqlite3.connect(str(target))
    try:
        idle.execute("INSERT INTO meta(key,value) VALUES('idle_probe','1')")
        idle.commit()
        # No open transaction: the connection is idle but alive.
        assert Path(str(target) + "-wal").exists()
        with pytest.raises(ValidationError, match="WAL/SHM"):
            with store.connect():
                pass
        # The idle connection's sidecars were left alone.
        assert Path(str(target) + "-wal").exists()
        assert Path(str(target) + "-shm").exists()
    finally:
        idle.close()


def test_connect_waits_out_a_briefly_held_store(tmp_path, monkeypatch):
    """A writer that closes within the settle window must not fail the open."""
    import threading

    _fast_settle(monkeypatch, attempts=40, interval=0.05)
    target = tmp_path / "imprint.db"
    store = ImprintStore(target)
    store.initialize()

    holder = sqlite3.connect(str(target), check_same_thread=False)
    holder.execute("INSERT INTO meta(key,value) VALUES('brief_probe','1')")
    holder.commit()
    assert Path(str(target) + "-wal").exists()

    release = threading.Timer(0.3, holder.close)
    release.start()
    try:
        with store.connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='brief_probe'"
            ).fetchone()
        assert row[0] == "1"
    finally:
        release.cancel()
        try:
            holder.close()
        except sqlite3.ProgrammingError:
            pass


def test_recover_refuses_when_a_live_writer_holds_the_store(tmp_path):
    target = tmp_path / "imprint.db"
    store = ImprintStore(target)
    store.initialize()

    live = sqlite3.connect(str(target))
    try:
        live.execute("INSERT INTO meta(key,value) VALUES('live_probe','1')")
        live.commit()
        # Hold an open read snapshot so the WAL cannot be reset by another connection.
        live.execute("BEGIN")
        live.execute("SELECT * FROM meta").fetchall()
        assert Path(str(target) + "-wal").exists()
        with pytest.raises(ValidationError, match="live writer"):
            store.recover()
    finally:
        live.close()


def test_recover_force_drains_non_replayable_sidecar_bytes(tmp_path):
    """Ordinary opens refuse unknown sidecar bytes; explicit recover may discard them."""
    target = tmp_path / "imprint.db"
    store = ImprintStore(target)
    store.initialize()
    Path(str(target) + "-wal").write_bytes(b"uncheckpointed-wal-sentinel")
    Path(str(target) + "-shm").write_bytes(b"shared-memory-sentinel")

    result = store.recover()
    assert result["status"] == "recovered"
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()
    assert store.integrity_check() == "ok"


def test_recover_on_a_clean_store_reports_clean(tmp_path):
    target = tmp_path / "imprint.db"
    store = ImprintStore(target)
    store.initialize()
    assert not Path(str(target) + "-wal").exists()

    result = store.recover()
    assert result["status"] == "clean"

    # The store still opens normally after a no-op recovery.
    with store.connect() as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='store_schema_version'"
        ).fetchone() is not None


def test_recover_requires_an_initialized_store(tmp_path):
    store = ImprintStore(tmp_path / "missing.db")
    with pytest.raises(ValidationError, match="initialized before recovery"):
        store.recover()
