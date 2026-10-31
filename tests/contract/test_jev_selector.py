"""Contract coverage for Jev selection wired into prompt retrieval (offline).

Every request goes to an in-process fake transport; the suite-wide network
guard in ``conftest.py`` makes any real connection fail.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import imprint.cli as cli
import imprint.retrieve as retrieval
from imprint.config import DEFAULTS
from imprint.retrieve import (
    jev_receipt_scope,
    retrieve_payload,
    select_prompt_payload,
)
from imprint.retrieve import jev
from imprint.retrieve.jev import QUESTION_PREFIX, STATE_LIMIT, STATEMENT_LIMIT
from imprint.retrieve.models import RetrievalRecord
from imprint.store import ImprintStore

KEY_ENV = "JEV_TEST_KEY"


@pytest.fixture(autouse=True)
def selector_environment(tmp_path, monkeypatch):
    """Isolate HOME/XDG so no real key file is reachable; remove the suite's
    deterministic override so these in-process tests can enable Jev."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")


class FakeTransport:
    def __init__(self, scorer=lambda instructions: 0.9, *, raw=None, error=None):
        self.calls: list[dict] = []
        self.scorer, self.raw, self.error = scorer, raw, error

    def __call__(self, endpoint, body, headers, timeout):
        request = json.loads(body)
        self.calls.append({"endpoint": endpoint, "body": request, "raw": body, "headers": dict(headers)})
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return json.dumps({
            "model": "jev-test-echo",
            "answers": {
                name: {"type": "noul", "noul": self.scorer(question["instructions"])}
                for name, question in request["questions"].items()
            },
            "usage": {"input_tokens": 40, "output_tokens": 4},
        }).encode("utf-8")

    def sent_statements(self, index: int = -1) -> set[str]:
        return {
            question["instructions"][len(QUESTION_PREFIX):]
            for question in self.calls[index]["body"]["questions"].values()
        }


@pytest.fixture
def transport(monkeypatch) -> FakeTransport:
    fake = FakeTransport()
    monkeypatch.setattr(jev, "http_post", fake)
    return fake


def selector_config(mode: str = "jev", **jev_overrides) -> dict:
    value = copy.deepcopy(DEFAULTS["selector"])
    value["mode"] = mode
    value["jev"]["api_key_env"] = KEY_ENV
    value["jev"].update(jev_overrides)
    return value


def record(record_id: str, statement: str, **overrides) -> RetrievalRecord:
    values = dict(
        record_id=record_id, text=statement, section="general",
        provenance_status="captured", authority_tier="captured_judgment",
        evidence_ids=("urn:imprint:evidence:synthetic-e1",), case_ids=("urn:imprint:case:synthetic-c1",),
        case_summaries=("Synthetic case summary for Example Holdings",), provenance_complete=True,
        ontology_type="Verdict", ontology_path=("judgment", "Verdict"), judgment_statement=statement,
    )
    values.update(overrides)
    return RetrievalRecord(**values)


class FakeStore:
    """Snapshot identity is the generation; candidates come from FakeSource."""

    def __init__(self):
        self.generation = 1

    def snapshot(self):
        return {"generation": self.generation}


@pytest.fixture
def fake_records(monkeypatch):
    holder: dict[str, list[RetrievalRecord]] = {"records": []}
    reads = []

    class FakeSource:
        def __init__(self, store):
            pass

        def retrieval_candidates(self, snapshot_id):
            reads.append(snapshot_id)
            return list(holder["records"])

    monkeypatch.setattr(retrieval, "StoreRetrievalSource", FakeSource)
    holder["reads"] = reads  # type: ignore[assignment]
    return holder


def _select(root, *, state="release safely", domain=None, budget=32768, mode="jev", refresh=False, store=None, **jev_overrides):
    return select_prompt_payload(
        store or FakeStore(), root=root, session_id="synthetic-session", state=state,
        explicit_domain=domain, budget=budget, selector=selector_config(mode, **jev_overrides),
        mode=mode, refresh=refresh,
    )


def _commit(root, decision):
    assert retrieval.commit_payload_delivery(
        root=root, session_id="synthetic-session", snapshot_id=str(decision.response["snapshot_id"]),
        domain_id=str(decision.response["receipt_scope"]),
    )


# -- eligibility and egress ----------------------------------------------------------

def test_only_eligible_selectable_judgments_are_sent(tmp_path, transport, fake_records):
    base = record("good", "ship safely")
    fake_records["records"] = [
        base,
        replace(base, record_id="principle", ontology_type="Principle"),
        replace(base, record_id="selected-domain", section="domain", domain_id="chosen"),
        replace(base, record_id="other-domain", section="domain", domain_id="other"),
        replace(base, record_id="excluded-domain", section="domain", domain_id="private"),
        replace(base, record_id="domain-escape", domain_id="other"),
        replace(base, record_id="rejected", rejected=True),
        replace(base, record_id="deleted", tombstoned=True),
        replace(base, record_id="old", current=False),
        replace(base, record_id="expired", valid_until="2020-01-01T00:00:00Z"),
        replace(base, record_id="no-proof", provenance_complete=False),
        replace(base, record_id="no-evidence", evidence_ids=()),
        replace(base, record_id="inferred", provenance_status="inferred", authority_tier="inferred_candidate"),
        replace(base, record_id="unselected-import", provenance_status="extracted", authority_tier="imported_floor"),
        replace(base, record_id="chosen-future", ontology_partition="chosen_future", ontology_type="ChosenFuture",
                provenance_status="ratified", authority_tier="ratified_knowledge", case_ids=()),
        replace(base, record_id="default-future", ontology_partition="default_future", ontology_type="DefaultFuture",
                provenance_status="inferred", authority_tier="inferred_candidate"),
        replace(base, record_id="price", ontology_partition="business_declared", ontology_type="Price",
                text="Price: 4200 USD", judgment_statement=None),
        replace(base, record_id="declared-claim", ontology_partition="business_declared", ontology_type="Claim"),
        replace(base, record_id="observation", ontology_partition="business_observed", ontology_type="Observation"),
        replace(base, record_id="self-model", ontology_partition="self_model", ontology_type="SelfModelAssertion",
                provenance_status="ratified", authority_tier="ratified_knowledge"),
        replace(base, record_id="ingested", ontology_type="IngestedItem"),
        replace(base, record_id="no-statement", judgment_statement=None),
    ]
    decision = _select(tmp_path, domain="chosen", exclude_domains=["private"])
    assert decision.method == "jev" and decision.outcome == "picked"
    sent = transport.calls[0]["body"]["questions"]
    assert len(sent) == 3
    assert set(decision.response["selected_ids"]) == {"good", "principle", "selected-domain"}
    assert decision.response["eligible_count"] == 3
    # Narrowing node_types narrows what leaves the machine, not what the
    # matched domain delivers: its unsent Verdict is still injected locally.
    decision = _select(tmp_path, state="different turn", domain="chosen", node_types=["Principle"])
    assert len(transport.calls[-1]["body"]["questions"]) == 1
    assert decision.response["selected_ids"] == ["selected-domain", "principle"]


def test_chosen_future_is_eligible_deterministically_but_never_sent(tmp_path, transport, fake_records):
    fake_records["records"] = [record(
        "chosen-future", "ship safely", ontology_partition="chosen_future", ontology_type="ChosenFuture",
        provenance_status="ratified", authority_tier="ratified_knowledge", case_ids=(), section="core",
    )]
    deterministic = retrieve_payload(FakeStore(), root=tmp_path, session_id="s", refresh=True)
    assert deterministic["selected_ids"] == ["chosen-future"]
    decision = _select(tmp_path)
    assert (decision.outcome, decision.fallback_reason) == ("fallback", "no_candidates")
    assert transport.calls == []


def test_egress_body_contains_no_identifiers_or_descriptive_fields(tmp_path, transport, fake_records):
    fake_records["records"] = [
        record(
            "urn:imprint:verdict:11111111-2222-4333-8444-555555555555",
            "Always show the failing source before the summary. " * 20,
            case_summaries=("Pricing review for Example Holdings at $4,200 in /srv/projects/example",),
            source_receipt_ids=("urn:imprint:evidence:synthetic-e1",),
            ontology_path=("judgment", "Verdict"),
        ),
        record("urn:imprint:rule:2", "Cite every failed source in the research domain.", ontology_type="Rule",
               provenance_status="ratified", authority_tier="ratified_knowledge", case_ids=()),
    ]
    _select(tmp_path, state="show the failing source")
    call = transport.calls[0]
    body = call["body"]
    assert set(body) == {"model", "state", "questions"}
    assert body["state"] == "show the failing source"
    assert set(body["questions"]) == {"c0", "c1"}
    raw = call["raw"].decode("utf-8")
    for forbidden in (
        "urn:imprint", "Example Holdings", "4,200", "/srv/projects", "synthetic-session",
        "evidence", '"judgment","Verdict"', "Verdict", "synthetic-test-key",
    ):
        assert forbidden not in raw
    for question in body["questions"].values():
        assert question["type"] == "noul"
        assert len(question["instructions"]) <= len(QUESTION_PREFIX) + STATEMENT_LIMIT


def test_state_is_bounded(tmp_path, transport, fake_records):
    fake_records["records"] = [record("a", "ship safely")]
    _select(tmp_path, state="ship " * 5000)
    assert len(transport.calls[0]["body"]["state"]) == STATE_LIMIT


# -- delivery, budget, receipts ----------------------------------------------------------

def test_picks_render_with_selector_metadata_in_score_order(tmp_path, monkeypatch, fake_records):
    fake_records["records"] = [record("low", "alpha"), record("high", "beta"), record("floor", "gamma")]
    scores = {"alpha": 0.7, "beta": 0.95, "gamma": 0.1}
    monkeypatch.setattr(jev, "http_post", FakeTransport(lambda text: scores[text[len(QUESTION_PREFIX):]]))
    decision = _select(tmp_path)
    lines = [json.loads(line) for line in decision.response["payload"].splitlines()]
    assert [line["record_id"] for line in lines] == ["high", "low"]
    assert lines[0]["selector"] == {"mode": "jev", "rank": 1, "score": 0.95}
    assert lines[1]["selector"] == {"mode": "jev", "rank": 2, "score": 0.7}
    assert lines[0]["evidence_ids"] and lines[0]["case_summaries"]
    assert decision.response["selected_by_partition"] == {"judgment": ["high", "low"]}
    assert sum(decision.response["section_bytes"].values()) == decision.response["selected_bytes"]


def test_picks_never_exceed_the_budget(tmp_path, transport, fake_records):
    fake_records["records"] = [record(f"r{index:02d}", f"large judgment {index} " + "x" * 1500) for index in range(20)]
    decision = _select(tmp_path, budget=4096)
    payload = decision.response["payload"].encode("utf-8")
    assert 0 < len(payload) <= 4096
    assert decision.response["selected_bytes"] == len(payload)
    assert decision.response["budget_bytes"] == 4096
    assert decision.response["omitted_count"] == 20 - len(decision.response["selected_ids"])
    # selector.jev.budget_bytes overrides the context budget, within the same bounds.
    decision = _select(tmp_path, state="other turn", budget=32768, budget_bytes=8192)
    assert decision.response["budget_bytes"] == 8192
    assert len(decision.response["payload"].encode("utf-8")) <= 8192


def test_an_oversized_pick_is_marked_compact_never_silently_cut(tmp_path, transport, fake_records):
    fake_records["records"] = [record("huge", "huge " + "y" * 6000), record("small", "small judgment")]
    decision = _select(tmp_path, budget=4096)
    assert len(decision.response["selected_ids"]) == 2
    lines = [json.loads(line) for line in decision.response["payload"].splitlines()]
    (cut,) = [line for line in lines if line.get("compact")]
    assert cut["text_complete"] is False
    assert cut["detail"] == f"imprint history {cut['record_id']}"


def test_a_lone_oversized_pick_still_reaches_the_turn(tmp_path, transport, fake_records):
    fake_records["records"] = [record("huge", "huge " + "y" * 6000)]
    decision = _select(tmp_path, budget=4096)
    assert (decision.method, decision.outcome) == ("jev", "picked")
    assert decision.response["selector_stages"]["delivered_compact"] == 1


def _whole_ids(decision):
    return [
        line["record_id"] for line in map(json.loads, decision.response["payload"].splitlines())
        if not line.get("compact")
    ]


def _compact_ids(decision):
    return [
        line["record_id"] for line in map(json.loads, decision.response["payload"].splitlines())
        if line.get("compact")
    ]


def _decisions(root):
    return [json.loads(line) for line in (root / "logs" / "jev-selector.jsonl").read_text().splitlines()]


def test_a_pick_compacted_for_room_reaches_a_later_turn_whole(tmp_path, transport, fake_records):
    fake_records["records"] = [record(f"r{index}", f"rule {index} " + "x" * 7000) for index in range(6)]
    first = _select(tmp_path, state="first turn")
    assert _whole_ids(first) == ["r0", "r1", "r2", "r3"]
    assert _compact_ids(first) == ["r4", "r5"]
    assert first.response["retry_ids"] == ["r4", "r5"]
    _commit(tmp_path, first)
    second = _select(tmp_path, state="second turn")
    assert len(transport.calls) == 2
    assert len(transport.sent_statements()) == 2
    assert _whole_ids(second) == ["r4", "r5"]
    assert _compact_ids(second) == []
    _commit(tmp_path, second)
    third = _select(tmp_path, state="third turn")
    assert third.outcome == "no_picks"
    assert len(transport.calls) == 2


def test_a_compact_pick_is_not_kept_from_the_deterministic_fallback(tmp_path, transport, fake_records):
    fake_records["records"] = [
        record(f"r{index}", f"rule {index} " + "x" * 7000, section="domain", domain_id="research")
        for index in range(6)
    ]
    first = _select(tmp_path, state="first turn", domain="research")
    assert _compact_ids(first) == ["r4", "r5"]
    _commit(tmp_path, first)
    fake_records["records"].append(record("new", "new rule", section="domain", domain_id="research"))
    store = FakeStore()
    store.generation = 2
    transport.error = TimeoutError("synthetic timeout")
    fallback = _select(tmp_path, state="second turn", domain="research", store=store)
    assert fallback.fallback_reason == "timeout"
    assert fallback.exclude_ids == frozenset({"r0", "r1", "r2", "r3"})
    delivered = retrieve_payload(
        store, root=tmp_path, session_id="synthetic-session", explicit_domain="research", refresh=True,
        context=fallback.context, exclude_ids=fallback.exclude_ids,
    )
    assert set(delivered["selected_ids"]) == {"new", "r4", "r5"}
    assert '"compact"' not in str(delivered["payload"])


def test_large_picks_jev_keeps_choosing_all_arrive_whole(tmp_path, transport, fake_records):
    fake_records["records"] = [record(f"r{index}", f"rule {index} " + "x" * 9000) for index in range(5)]
    whole = []
    for turn in range(4):
        decision = _select(tmp_path, state=f"turn {turn}")
        whole.extend(_whole_ids(decision))
        if decision.outcome != "picked":
            break
        _commit(tmp_path, decision)
    assert sorted(whole) == ["r0", "r1", "r2", "r3", "r4"]
    assert decision.outcome == "no_picks"


def test_two_picks_near_the_whole_budget_still_arrive_whole(tmp_path, transport, fake_records):
    fake_records["records"] = [record(name, f"rule {name} " + "x" * 31000) for name in ("a", "b")]
    whole = []
    for turn in range(4):
        decision = _select(tmp_path, state=f"turn {turn}")
        whole.extend(_whole_ids(decision))
        if decision.outcome != "picked":
            break
        _commit(tmp_path, decision)
    assert sorted(whole) == ["a", "b"]
    assert decision.outcome == "no_picks"


def test_a_pick_that_can_never_fit_whole_is_delivered_once(tmp_path, transport, fake_records):
    fake_records["records"] = [record("huge", "huge " + "y" * 6000)]
    first = _select(tmp_path, state="first turn", budget=4096)
    assert _compact_ids(first) == ["huge"]
    assert first.response["retry_ids"] == []
    _commit(tmp_path, first)
    second = _select(tmp_path, state="second turn", budget=4096)
    assert second.outcome == "no_picks"
    assert len(transport.calls) == 1


def test_a_whole_delivery_settles_a_record_an_older_receipt_retried(tmp_path, transport, fake_records):
    fake_records["records"] = [record(f"r{index}", f"rule {index} " + "x" * 7000) for index in range(6)]
    first = _select(tmp_path, state="first turn")
    _commit(tmp_path, first)
    _commit(tmp_path, _select(tmp_path, state="second turn"))
    receipts = retrieval.DeliveryReceipts(tmp_path / "receipts")
    session = retrieval._session_ref("synthetic-session")
    assert receipts.delivered_ids(session) == {f"r{index}" for index in range(6)}


def test_a_receipt_written_before_retry_ids_still_latches(tmp_path, transport, fake_records):
    fake_records["records"] = [record("a", "alpha rule")]
    decision = _select(tmp_path)
    decision.response.pop("retry_ids", None)
    receipts = retrieval.DeliveryReceipts(tmp_path / "receipts")
    session = retrieval._session_ref("synthetic-session")
    scope = str(decision.response["receipt_scope"])
    for path in (tmp_path / "receipts" / session).glob("*.pending.json"):
        path.unlink()
    receipts.prepare_delivery(session, str(decision.response["snapshot_id"]), scope, decision.response)
    _commit(tmp_path, decision)
    assert receipts.delivery_state(session) == ({"a"}, set())


def test_the_log_records_the_delivery_outcome_beside_jevs(tmp_path, transport, fake_records):
    evidence = tuple(f"urn:imprint:evidence:synthetic-{index}" for index in range(200))
    fake_records["records"] = [record("wide", "wide rule", evidence_ids=evidence)]
    decision = _select(tmp_path, budget=4096)
    assert decision.outcome == "oversized_picks"
    (line,) = _decisions(tmp_path)
    assert (line["outcome"], line["delivery_outcome"]) == ("picked", "oversized_picks")
    assert line["stages"]["unfit"] == 1
    summary = jev.decision_log_summary(tmp_path)
    assert summary["last_success_ts"] == line["ts"]


def test_the_log_shows_local_records_crowding_out_a_pick(tmp_path, transport, fake_records):
    fake_records["records"] = [
        record(f"local{index:02d}", "local rule " + "x" * 900, section="domain", domain_id="private")
        for index in range(20)
    ] + [record("pick", "small rule")]
    decision = _select(tmp_path, domain="private", exclude_domains=["private"], budget=4096)
    assert decision.outcome == "crowded_picks"
    (line,) = _decisions(tmp_path)
    assert line["delivery_outcome"] == "crowded_picks"
    assert line["stages"]["local_records"] == len(decision.response["selected_ids"]) > 0
    assert line["stages"]["local_bytes"] == decision.response["selected_bytes"]
    assert decision.response["selected_ids"] == [
        record_id for record_id in decision.response["selected_ids"] if record_id.startswith("local")
    ]


def test_shadow_and_fallback_lines_have_no_delivery_to_report(tmp_path, transport, fake_records, monkeypatch):
    fake_records["records"] = [record("a", "ship safely")]
    _select(tmp_path, mode="shadow")
    monkeypatch.delenv(KEY_ENV)
    _select(tmp_path, state="other turn")
    for line in _decisions(tmp_path):
        assert (line["stages"], line["delivery_outcome"]) == (None, None)


def test_never_sent_domain_records_are_delivered_locally_ahead_of_picks(tmp_path, transport, fake_records):
    """exclude_domains and node_types bound egress, not local delivery."""
    fake_records["records"] = [
        record("general", "ship safely"),
        record("private-rule", "private domain rule", section="domain", domain_id="private"),
        record("private-price", "Price: 4200 USD", section="domain", domain_id="private",
               ontology_partition="business_declared", ontology_type="Price", judgment_statement=None),
    ]
    decision = _select(tmp_path, domain="private", exclude_domains=["private"])
    assert transport.sent_statements() == {"ship safely"}
    assert decision.outcome == "picked"
    lines = [json.loads(line) for line in decision.response["payload"].splitlines()]
    assert [line["record_id"] for line in lines] == ["private-price", "private-rule", "general"]
    assert ["selector" in line for line in lines] == [False, False, True]
    assert decision.response["eligible_count"] == 3
    assert sum(decision.response["section_bytes"].values()) == decision.response["selected_bytes"]
    # Jev answering "nothing applies" still leaves the domain's own records.
    transport.scorer = lambda _: 0.05
    quiet = _select(tmp_path, state="another turn", domain="private", exclude_domains=["private"])
    assert quiet.outcome == "no_picks"
    assert quiet.response["selected_ids"] == ["private-price", "private-rule"]
    # They count as delivered for the session afterward.
    _commit(tmp_path, quiet)
    transport.scorer = lambda _: 0.9
    later = _select(tmp_path, state="third turn", domain="private", exclude_domains=["private"])
    assert later.response["selected_ids"] == ["general"]


def test_no_picks_injects_nothing(tmp_path, monkeypatch, fake_records):
    fake_records["records"] = [record("a", "ship safely")]
    monkeypatch.setattr(jev, "http_post", FakeTransport(lambda _: 0.1))
    decision = _select(tmp_path)
    assert decision.method == "jev" and decision.outcome == "no_picks"
    assert decision.response["status"] == "delivered"
    assert decision.response["payload"] == ""
    assert decision.response["selected_ids"] == []


def test_same_turn_replays_its_receipt_without_a_second_request(tmp_path, transport, fake_records):
    fake_records["records"] = [record("a", "ship safely")]
    first = _select(tmp_path)
    replay = _select(tmp_path)
    assert replay.response == first.response
    assert len(transport.calls) == 1
    _commit(tmp_path, first)
    again = _select(tmp_path)
    assert again.response["status"] == "already_delivered"
    assert again.outcome == "already_delivered"
    assert len(transport.calls) == 1
    receipt = (
        tmp_path / "receipts" / retrieval._session_ref("synthetic-session")
    ).glob(f"*-domain-{first.response['receipt_scope']}.json")
    assert len(list(receipt)) == 1


def test_receipt_scope_is_a_safe_receipt_id_bound_to_state_and_domain():
    from imprint.retrieve.receipts import _SAFE

    scope = jev_receipt_scope("ship safely", None)
    assert scope.startswith("jev-") and len(scope) == 28 and _SAFE.fullmatch(scope)
    assert jev_receipt_scope("ship safely", "research") != scope
    assert jev_receipt_scope("ship safely", None, True) != scope
    assert jev_receipt_scope("ship safely!", None) != scope


def test_delivered_ids_are_excluded_across_a_mid_session_store_change(tmp_path, transport, fake_records):
    store = FakeStore()
    fake_records["records"] = [record("a", "alpha rule"), record("b", "beta rule")]
    transport.scorer = lambda text: 0.9 if "alpha" in text else 0.1
    first = _select(tmp_path, state="alpha", store=store)
    assert first.response["selected_ids"] == ["a"]
    _commit(tmp_path, first)
    store.generation = 2  # the store changed: a new snapshot id
    fake_records["records"] = [record("a", "alpha rule"), record("b", "beta rule"), record("c", "alpha again")]
    second = _select(tmp_path, state="alpha", store=store)
    assert second.response["snapshot_id"] != first.response["snapshot_id"]
    sent = transport.sent_statements()
    assert "alpha rule" not in sent and sent == {"beta rule", "alpha again"}
    assert second.response["selected_ids"] == ["c"]


def test_deterministic_session_start_deliveries_are_excluded(tmp_path, transport, fake_records):
    fake_records["records"] = [
        record("seed", "brief rule"),
        record("domain-rule", "alpha domain rule", section="domain", domain_id="research"),
    ]
    store = FakeStore()
    start = retrieve_payload(store, root=tmp_path, session_id="synthetic-session")
    assert start["selected_ids"] == ["seed"]
    assert retrieval.commit_payload_delivery(
        root=tmp_path, session_id="synthetic-session", snapshot_id=start["snapshot_id"],
    )
    decision = _select(tmp_path, state="alpha", store=store, domain="research")
    assert transport.sent_statements() == {"alpha domain rule"}
    assert decision.response["selected_ids"] == ["domain-rule"]
    # With everything already delivered there is nothing to send at all.
    _commit(tmp_path, decision)
    # Nothing is repeated and nothing falls back to re-injecting it.
    empty = _select(tmp_path, state="alpha again", store=store, domain="research")
    assert (empty.method, empty.outcome, empty.fallback_reason) == ("jev", "no_picks", None)
    assert empty.response["payload"] == ""
    assert len(transport.calls) == 1


def test_corrupt_and_foreign_receipts_are_ignored(tmp_path, transport, fake_records):
    fake_records["records"] = [record("a", "alpha rule")]
    directory = tmp_path / "receipts" / retrieval._session_ref("synthetic-session")
    directory.mkdir(parents=True)
    (directory / "corrupt-session-start.json").write_text("{not json")
    (directory / "foreign-session-start.json").write_text(json.dumps({"receipt_schema_version": "9", "response": []}))
    (directory / "list.json").write_text("[1, 2, 3]")
    (directory / "nested.json").write_bytes(b"[" * 200000)
    (directory / "tampered.json").write_text(json.dumps({
        "receipt_schema_version": "1.1.0", "response_sha256": "0" * 64,
        "response": {"selected_ids": ["a"], "payload": "", "budget_bytes": 1},
    }))
    (directory / "binary.json").write_bytes(b"\xff\xfe\x00")
    decision = _select(tmp_path, state="alpha")
    assert decision.method == "jev"
    assert transport.sent_statements() == {"alpha rule"}


def test_delivered_id_scan_is_bounded(tmp_path, fake_records):
    from imprint.retrieve.receipts import DeliveryReceipts

    receipts = DeliveryReceipts(tmp_path / "receipts")
    session = retrieval._session_ref("synthetic-session")
    for index in range(5):
        response = {"payload": "", "budget_bytes": 4096, "selected_ids": [f"r{index}"]}
        receipts.prepare_delivery(session, f"snapshot-{index}", None, response)
        receipts.commit_delivery(session, f"snapshot-{index}", None)
        path = next((tmp_path / "receipts" / session).glob(f"{__import__('hashlib').sha256(f'snapshot-{index}'.encode()).hexdigest()[:24]}-*.json"))
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    assert receipts.delivered_ids(session) == {f"r{index}" for index in range(5)}
    assert receipts.delivered_ids(session, max_files=2) == {"r4", "r3"}
    assert receipts.delivered_ids(session, max_bytes=10) == set()


# -- fallback and shadow ----------------------------------------------------------------

def test_missing_key_falls_back_without_a_request(tmp_path, transport, fake_records, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    fake_records["records"] = [record("a", "ship safely")]
    decision = _select(tmp_path)
    assert (decision.method, decision.outcome, decision.fallback_reason) == (
        "deterministic_fallback", "fallback", "missing_key",
    )
    assert decision.response is None
    assert transport.calls == []


def test_fallback_leaves_no_jev_receipt(tmp_path, monkeypatch, fake_records):
    fake_records["records"] = [record("a", "ship safely")]
    monkeypatch.setattr(jev, "http_post", FakeTransport(raw=b"not json"))
    decision = _select(tmp_path)
    assert decision.fallback_reason == "bad_response"
    assert not list((tmp_path / "receipts").rglob("*jev-*"))


@pytest.mark.parametrize("target", ["_render", "_jev_response"])
def test_exception_in_render_or_budget_falls_back(tmp_path, transport, fake_records, monkeypatch, target):
    fake_records["records"] = [record("a", "ship safely")]

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(retrieval, target, boom)
    decision = _select(tmp_path)
    assert (decision.method, decision.outcome, decision.fallback_reason) == (
        "deterministic_fallback", "fallback", "exception",
    )
    assert decision.response is None


def test_exception_reading_receipts_falls_back(tmp_path, transport, fake_records, monkeypatch):
    fake_records["records"] = [record("a", "ship safely")]
    from imprint.retrieve.receipts import DeliveryReceipts

    def boom(self, *args, **kwargs):
        raise OSError("synthetic receipt failure")

    monkeypatch.setattr(DeliveryReceipts, "delivery_state", boom)
    decision = _select(tmp_path)
    assert decision.fallback_reason == "exception"


def test_fallback_context_reuses_the_single_store_read(tmp_path, fake_records, monkeypatch):
    fake_records["records"] = [record("a", "ship safely", section="domain", domain_id="research")]
    monkeypatch.delenv(KEY_ENV)
    decision = _select(tmp_path, domain="research")
    assert decision.fallback_reason == "missing_key"
    result = retrieve_payload(
        FakeStore(), root=tmp_path, session_id="synthetic-session", explicit_domain="research",
        domain_only=True, context=decision.context,
    )
    assert result["selected_ids"] == ["a"]
    assert len(fake_records["reads"]) == 1


def test_shadow_calls_and_logs_but_never_delivers(tmp_path, transport, fake_records):
    fake_records["records"] = [record("a", "ship safely")]
    decision = _select(tmp_path, mode="shadow")
    assert (decision.method, decision.outcome, decision.response) == ("shadow", "picked", None)
    assert len(transport.calls) == 1
    assert not (tmp_path / "receipts").exists() or not list((tmp_path / "receipts").rglob("*.json"))
    lines = (tmp_path / "logs" / "jev-selector.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["mode"] == "shadow" and entry["outcome"] == "picked"
    assert entry["picks"] == [{"record_id": "a", "score": 0.9}]


# -- decision log --------------------------------------------------------------------

def test_decision_log_is_content_free_and_private(tmp_path, transport, fake_records, monkeypatch):
    statement = "Synthetic judgment text that must stay local"
    prompt = "Synthetic prompt text that must stay local"
    fake_records["records"] = [record("urn:imprint:verdict:a", statement)]
    _select(tmp_path, state=prompt)
    monkeypatch.delenv(KEY_ENV)
    _select(tmp_path, state=prompt + " again")
    path = tmp_path / "logs" / "jev-selector.jsonl"
    content = path.read_text()
    for forbidden in (statement, prompt, "synthetic-test-key", "synthetic-session", "Example Holdings"):
        assert forbidden not in content
    first, second = (json.loads(line) for line in content.splitlines())
    assert set(first) == {
        "candidates_sent", "delivery_outcome", "elapsed_ms", "fallback_reason", "mode", "model_echoed", "outcome",
        "picks", "receipt_scope", "request_bytes", "request_sent", "session", "stages", "ts", "usage",
    }
    assert first["session"] == retrieval._session_ref("synthetic-session")
    assert first["receipt_scope"].startswith("jev-")
    assert first["picks"] == [{"record_id": "urn:imprint:verdict:a", "score": 0.9}]
    assert first["usage"] == {"input_tokens": 40, "output_tokens": 4}
    assert first["model_echoed"] == "jev-test-echo"
    assert (second["outcome"], second["fallback_reason"], second["request_sent"]) == ("fallback", "missing_key", False)
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0


def test_decision_log_can_be_disabled(tmp_path, transport, fake_records):
    fake_records["records"] = [record("a", "ship safely")]
    _select(tmp_path, log_decisions=False)
    assert not (tmp_path / "logs" / "jev-selector.jsonl").exists()


def test_a_failure_after_a_successful_call_logs_one_fallback(tmp_path, transport, fake_records, monkeypatch):
    fake_records["records"] = [record("a", "ship safely")]

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(retrieval, "_jev_response", boom)
    decision = _select(tmp_path)
    assert (decision.method, decision.fallback_reason) == ("deterministic_fallback", "exception")
    lines = [json.loads(line) for line in jev.decision_log_path(tmp_path).read_text().splitlines()]
    assert len(lines) == 1
    assert (lines[0]["outcome"], lines[0]["fallback_reason"], lines[0]["request_sent"]) == (
        "fallback", "exception", True,
    )
    assert lines[0]["picks"] == []
    health = jev.selector_health(tmp_path, selector_config("jev"))
    assert (health["network_state"], health["last_success_ts"], health["fallbacks_24h"]) == (
        "transferred", "", 1,
    )


def test_decision_log_rotates_at_its_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "DECISION_LOG_MAX_BYTES", 400)
    entry = {"outcome": "fallback", "ts": "2026-10-29T00:00:00Z", "padding": "x" * 100}
    for _ in range(10):
        jev.log_decision(tmp_path, entry)
    current = tmp_path / "logs" / "jev-selector.jsonl"
    rotated = tmp_path / "logs" / "jev-selector.jsonl.1"
    assert current.stat().st_size <= 400 and rotated.stat().st_size <= 400


# -- health --------------------------------------------------------------------------

def test_health_network_state_is_truthful(tmp_path, transport, fake_records, monkeypatch):
    from imprint.health import health_report

    root = tmp_path / "operator"  # private tree; tmp_path itself is not
    config = {**copy.deepcopy(DEFAULTS), "selector": selector_config("deterministic")}
    store = ImprintStore(root / "imprint.db")
    metrics = health_report(root, store, config)["metrics"]
    assert metrics["network_state"] == "offline"
    assert metrics["selector_mode"] == "deterministic"
    assert metrics["selector_key_present"] is True
    assert metrics["selector_endpoint_host"] == "api.typesafe.ai"

    config["selector"]["mode"] = "jev"
    metrics = health_report(root, store, config)["metrics"]
    assert (metrics["network_state"], metrics["last_transfer_age_seconds"]) == ("idle", -1)

    fake_records["records"] = [record("a", "ship safely")]
    _commit(root, _select(root))  # the hook commits after flushing the payload
    fake_records["records"].append(record("b", "review safely"))  # something left to send
    monkeypatch.delenv(KEY_ENV)
    _select(root, state="another turn")
    report = health_report(root, store, config)
    metrics = report["metrics"]
    assert metrics["network_state"] == "transferred"
    assert 0 <= metrics["last_transfer_age_seconds"] < 120
    assert metrics["selector_key_present"] is False
    assert metrics["selector_fallbacks_24h"] == 1
    assert metrics["selector_last_success_ts"].endswith("Z")
    # The decision log and selector receipts are private operator state.
    assert "unsafe_permissions" not in report["degraded_reasons"]
    assert "abandoned_temp_present" not in report["degraded_reasons"]

    monkeypatch.setenv("IMPRINT_SELECTOR", "off")
    assert health_report(root, store, config)["metrics"]["network_state"] == "offline"


def test_health_sees_transfers_with_the_decision_log_disabled(tmp_path, transport, fake_records):
    root = tmp_path / "operator"
    fake_records["records"] = [record("a", "ship safely")]
    decision = _select(root, log_decisions=False)
    assert decision.outcome == "picked" and len(transport.calls) == 1
    assert not jev.decision_log_path(root).exists()
    health = jev.selector_health(root, selector_config("jev", log_decisions=False))
    assert health["network_state"] == "transferred"
    assert 0 <= health["last_transfer_age_seconds"] < 120
    assert health["last_success_ts"].endswith("Z")
    # The marker is content-free and private.
    marker = jev.transfer_marker_path(root)
    assert set(json.loads(marker.read_text())) == {"last_success_ts", "last_transfer_ts"}
    if os.name != "nt":
        assert marker.stat().st_mode & 0o077 == 0


def test_health_reads_across_a_log_rotation(tmp_path, monkeypatch):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    sent = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(jev, "DECISION_LOG_MAX_BYTES", 300)
    jev.log_decision(tmp_path, {"ts": sent, "outcome": "picked", "request_sent": True, "pad": "x" * 40})
    for _ in range(3):
        jev.log_decision(tmp_path, {"ts": recent, "outcome": "fallback", "request_sent": False, "pad": "x" * 40})
    assert (tmp_path / "logs" / "jev-selector.jsonl.1").exists()
    summary = jev.decision_log_summary(tmp_path, now=now)
    assert summary == {"fallbacks_24h": 3, "last_success_ts": sent, "last_transfer_ts": sent}


def test_health_counts_only_recent_fallbacks(tmp_path):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for stamp, outcome in ((old, "fallback"), (recent, "fallback"), (recent, "picked")):
        jev.log_decision(tmp_path, {"ts": stamp, "outcome": outcome, "request_sent": outcome == "picked"})
    (tmp_path / "logs" / "jev-selector.jsonl").open("ab").write(b"{partial")
    summary = jev.decision_log_summary(tmp_path, now=now)
    assert summary == {"fallbacks_24h": 1, "last_success_ts": recent, "last_transfer_ts": recent}


# -- hooks (in-process CLI) ------------------------------------------------------------

def _config(tmp_path: Path, *, selector: dict | None = None, budget: int = 32768) -> Path:
    path = tmp_path / "config.json"
    value = {
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"),
        "operator_slug": "test", "node_id": "primary", "compiler": True,
        "context_budget_bytes": budget,
        "domains": [{
            "domain_id": "research", "public_label": "Research",
            "safe_paths": ["Projects/Research"], "keywords": ["sources"], "frozen": False,
        }],
    }
    if selector is not None:
        value["selector"] = selector
    path.write_text(json.dumps(value))
    return path


def _set_selector(config: Path, selector: dict | None) -> None:
    value = json.loads(config.read_text())
    if selector is None:
        value.pop("selector", None)
    else:
        value["selector"] = selector
    config.write_text(json.dumps(value))


def _hook(config: Path, action: str, event: dict, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    code = cli.main(["--config", str(config), "hook", action])
    body = json.loads(capsys.readouterr().out)
    assert code == 0, body
    return body


def _seed(config: Path, monkeypatch, capsys) -> ImprintStore:
    for index, text in enumerate((
        "No, report the failed source because omissions change the decision.",
        "No, never rename a public flag without a migration note because users lose settings.",
    )):
        receipt = _hook(config, "stop-capture", {
            "session_id": f"seed-{index}", "operator_text": text,
            "case_description": "Pricing review for Example Holdings at $4,200 in /srv/projects/example",
        }, monkeypatch, capsys)
        assert receipt["canonical_status"] == "compiled", receipt
    store = ImprintStore(Path(json.loads(config.read_text())["data_root"]) / "test" / "imprint.db")
    evidence_id = store.current_nodes(["Evidence"])[0]["node_id"]
    rule_id = store.append_derived_node(
        node_type="Rule",
        payload={"statement": "Cite every failed source in the research domain.", "domain_id": "research"},
        provenance_status="inferred", authority_tier="inferred_candidate",
        evidence_ids=[evidence_id], operator_id=store.current_nodes(["Verdict"])[0]["operator_id"],
        valid_from="2026-07-14T12:00:00Z", proposed_by="jev-contract-test",
    )
    store.ratify_node(rule_id, ratifier="synthetic-operator")
    return store


def _strip_selector(body: dict) -> dict:
    return {key: value for key, value in body.items() if key not in {"selector_method", "selector_outcome", "fallback_reason"}}


def test_deterministic_default_makes_zero_requests_and_keeps_contract(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    start = _hook(config, "session-start", {"session_id": "s1"}, monkeypatch, capsys)
    assert start["status"] == "delivered"
    skipped = _hook(config, "user-prompt-submit", {"session_id": "s1", "prompt": "hello there"}, monkeypatch, capsys)
    assert skipped["status"] == "skipped" and skipped["reason"] == "domain_no_match"
    assert skipped["selector_method"] == "deterministic"
    domain = _hook(config, "user-prompt-submit", {
        "session_id": "s1", "cwd": "Projects/Research/Current", "prompt": "review",
    }, monkeypatch, capsys)
    assert domain["selection_method"] == "path" and domain["selector_method"] == "deterministic"
    assert "research domain" in domain["hookSpecificOutput"]["additionalContext"]
    assert transport.calls == []
    assert not (tmp_path / "data" / "test" / "logs" / "jev-selector.jsonl").exists()


def test_env_off_forces_zero_requests(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"))
    _seed(config, monkeypatch, capsys)
    monkeypatch.setenv("IMPRINT_SELECTOR", "off")
    body = _hook(config, "user-prompt-submit", {"session_id": "s1", "prompt": "failed source"}, monkeypatch, capsys)
    assert body["status"] == "skipped" and body["selector_method"] == "deterministic"
    assert transport.calls == []


def test_session_start_never_calls_jev(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"))
    _seed(config, monkeypatch, capsys)
    for source in ("startup", "compact", "resume", "clear"):
        _hook(config, "session-start", {"session_id": "s1", "source": source}, monkeypatch, capsys)
    assert transport.calls == []


def test_jev_prompt_selects_without_a_domain_and_dedups(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"), budget=4096)
    _seed(config, monkeypatch, capsys)
    transport.scorer = lambda text: 0.95 if "rename a public flag" in text else 0.1
    first = _hook(config, "user-prompt-submit", {"session_id": "s1", "prompt": "rename the flag"}, monkeypatch, capsys)
    assert first["status"] == "delivered"
    assert (first["selector_method"], first["selector_outcome"]) == ("jev", "picked")
    assert first["domain_id"] is None
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "rename a public flag" in context and '"selector":{"mode":"jev"' in context
    replay = _hook(config, "user-prompt-submit", {"session_id": "s1", "prompt": "rename the flag"}, monkeypatch, capsys)
    assert replay["status"] == "already_delivered" and replay["hookSpecificOutput"]["additionalContext"] == ""
    assert len(transport.calls) == 1
    _hook(config, "user-prompt-submit", {"session_id": "s1", "prompt": "flag again please"}, monkeypatch, capsys)
    assert all("rename a public flag" not in text for text in transport.sent_statements())


def test_jev_fallback_is_exactly_todays_behavior(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    events = [
        {"session_id": "PLACEHOLDER", "prompt": "hello there"},
        {"session_id": "PLACEHOLDER", "cwd": "Projects/Research/Current", "prompt": "review"},
        {"session_id": "PLACEHOLDER", "cwd": "Projects/Research/Current", "prompt": "review"},
    ]
    expected = [
        _hook(config, "user-prompt-submit", {**event, "session_id": "today"}, monkeypatch, capsys)
        for event in events
    ]
    _set_selector(config, selector_config("jev"))
    monkeypatch.delenv(KEY_ENV)
    fallback = [
        _hook(config, "user-prompt-submit", {**event, "session_id": "with-jev"}, monkeypatch, capsys)
        for event in events
    ]
    assert [_strip_selector(body) for body in fallback] == [_strip_selector(body) for body in expected]
    assert [body["status"] for body in fallback] == ["skipped", "delivered", "already_delivered"]
    assert all(body["selector_method"] == "deterministic_fallback" for body in fallback)
    assert all(body["fallback_reason"] == "missing_key" for body in fallback)
    assert transport.calls == []


def test_jev_exception_in_render_is_todays_behavior(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"))
    _seed(config, monkeypatch, capsys)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(retrieval, "_jev_response", boom)
    no_domain = _hook(config, "user-prompt-submit", {"session_id": "s1", "prompt": "failed source"}, monkeypatch, capsys)
    assert no_domain["status"] == "skipped" and no_domain["fallback_reason"] == "exception"
    domain = _hook(config, "user-prompt-submit", {
        "session_id": "s1", "cwd": "Projects/Research/Current", "prompt": "review",
    }, monkeypatch, capsys)
    assert domain["status"] == "delivered" and domain["selector_method"] == "deterministic_fallback"
    assert "research domain" in domain["hookSpecificOutput"]["additionalContext"]


def test_shadow_hook_delivers_todays_behavior_and_logs(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    event = {"cwd": "Projects/Research/Current", "prompt": "review the failed source"}
    expected = _hook(config, "user-prompt-submit", {**event, "session_id": "today"}, monkeypatch, capsys)
    _set_selector(config, selector_config("shadow"))
    shadow = _hook(config, "user-prompt-submit", {**event, "session_id": "shadowed"}, monkeypatch, capsys)
    assert _strip_selector(shadow) == _strip_selector(expected)
    assert (shadow["selector_method"], shadow["selector_outcome"]) == ("shadow", "picked")
    assert len(transport.calls) == 1
    log = tmp_path / "data" / "test" / "logs" / "jev-selector.jsonl"
    assert json.loads(log.read_text().splitlines()[-1])["mode"] == "shadow"


def test_no_picks_hook_injects_nothing(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"))
    _seed(config, monkeypatch, capsys)
    transport.scorer = lambda _: 0.05
    body = _hook(config, "user-prompt-submit", {
        "session_id": "s1", "cwd": "Projects/Research/Current", "prompt": "review",
    }, monkeypatch, capsys)
    assert (body["status"], body["selector_outcome"]) == ("delivered", "no_picks")
    assert body["hookSpecificOutput"]["additionalContext"] == ""


def test_excluded_or_unselected_domain_records_are_still_injected(tmp_path, transport, monkeypatch, capsys):
    """A setting that means "never send" must not mean "never deliver"."""
    rule = "Cite every failed source in the research domain."
    event = {"cwd": "Projects/Research/Current", "prompt": "review the failed source"}
    for name, overrides in (("excluded", {"exclude_domains": ["research"]}), ("narrowed", {"node_types": ["Verdict"]})):
        (tmp_path / name).mkdir()
        config = _config(tmp_path / name, selector=selector_config("jev", **overrides))
        _seed(config, monkeypatch, capsys)
        for scorer, outcome, count in ((lambda _: 0.9, "picked", 3), (lambda _: 0.05, "no_picks", 1)):
            transport.scorer = scorer
            body = _hook(config, "user-prompt-submit", {**event, "session_id": f"{name}-{outcome}"}, monkeypatch, capsys)
            assert (body["status"], body["selector_method"], body["selector_outcome"]) == ("delivered", "jev", outcome)
            assert rule not in transport.sent_statements()
            lines = [json.loads(line) for line in body["hookSpecificOutput"]["additionalContext"].splitlines()]
            assert len(lines) == count
            assert lines[0]["text"] == rule and "selector" not in lines[0]
            assert all("selector" in line for line in lines[1:])


def test_hook_selector_deadline_stays_inside_the_bridge_budget(tmp_path, monkeypatch, capsys):
    """timeout_seconds may be 30, but the hook bridge kills its child at 10."""
    release = threading.Event()
    calls = []

    def hang(endpoint, body, headers, timeout):
        calls.append(timeout)
        release.wait(60)
        raise TimeoutError

    config = _config(tmp_path, selector=selector_config("jev", timeout_seconds=30))
    _seed(config, monkeypatch, capsys)
    monkeypatch.setattr(jev, "http_post", hang)
    monkeypatch.setattr(cli, "HOOK_SELECTOR_BUDGET_SECONDS", 0.5)
    started = time.monotonic()
    try:
        body = _hook(config, "user-prompt-submit", {
            "session_id": "s1", "cwd": "Projects/Research/Current", "prompt": "review",
        }, monkeypatch, capsys)
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert len(calls) == 1 and calls[0] <= 0.5
    assert (body["status"], body["selector_method"], body["fallback_reason"]) == (
        "delivered", "deterministic_fallback", "timeout",
    )
    assert "research domain" in body["hookSpecificOutput"]["additionalContext"]


def test_hook_selector_budget_leaves_the_fallback_room_inside_the_bridge():
    spec = importlib.util.spec_from_file_location(
        "imprint_jev_budget_bridge", Path(__file__).resolve().parents[2] / "hooks" / "_bridge.py",
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    assert 0 < cli.HOOK_SELECTOR_BUDGET_SECONDS <= bridge.HOOK_TIMEOUT_SECONDS - 4


def test_an_unimportable_selector_module_is_todays_behavior(tmp_path, transport, monkeypatch, capsys):
    enabled = selector_config("jev", short_prompt_context={"enabled": True, "below_chars": 200, "max_chars": 100})
    config = _config(tmp_path, selector=enabled)
    _seed(config, monkeypatch, capsys)
    monkeypatch.setitem(sys.modules, "imprint.retrieve.jev", None)
    body = _hook(config, "user-prompt-submit", {
        "session_id": "s1", "cwd": "Projects/Research/Current", "prompt": "review",
        "transcript_path": str(tmp_path / "absent.jsonl"),
    }, monkeypatch, capsys)
    assert (body["status"], body["selector_method"], body["fallback_reason"]) == (
        "delivered", "deterministic_fallback", "exception",
    )
    assert "research domain" in body["hookSpecificOutput"]["additionalContext"]
    assert cli.main(["--config", str(config), "retrieve", "--session", "cli", "--prompt", "failed source"]) == 0
    retrieved = json.loads(capsys.readouterr().out)
    assert (retrieved["status"], retrieved["fallback_reason"]) == ("delivered", "exception")
    assert transport.calls == []


def test_hook_egress_from_a_real_store_carries_only_statements(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"))
    store = _seed(config, monkeypatch, capsys)
    _hook(config, "user-prompt-submit", {
        "session_id": "native-session-id-123", "cwd": "Projects/Research/Current", "prompt": "failed source",
    }, monkeypatch, capsys)
    raw = transport.calls[0]["raw"].decode("utf-8")
    for forbidden in ("urn:imprint", "Example Holdings", "4,200", "/srv/projects", "native-session-id-123", "Projects/Research"):
        assert forbidden not in raw
    sent = transport.sent_statements()
    verdicts = {node["payload"]["raw_operator_text"] for node in store.current_nodes(["Verdict"])}
    assert sent == verdicts | {"Cite every failed source in the research domain."}


def test_compact_and_resume_reset_receipts_only_with_a_selector(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    receipts = tmp_path / "data" / "test" / "receipts"
    _hook(config, "session-start", {"session_id": "s1", "source": "startup"}, monkeypatch, capsys)
    _hook(config, "user-prompt-submit", {"session_id": "s1", "cwd": "Projects/Research/Current", "prompt": "review"},
          monkeypatch, capsys)
    before = sorted(path.name for path in receipts.rglob("*.json"))
    assert len(before) == 2
    for source in ("compact", "resume"):
        body = _hook(config, "session-start", {"session_id": "s1", "source": source}, monkeypatch, capsys)
        assert body["status"] == "delivered"  # I4 refresh, latch untouched
        assert sorted(path.name for path in receipts.rglob("*.json")) == before

    _set_selector(config, selector_config("jev"))
    transport.scorer = lambda _: 0.9
    session_dir = receipts / retrieval._session_ref(
        cli._opaque_session_urn(tmp_path / "data" / "test", "s2"),
    )
    event = {"session_id": "s2", "cwd": "Projects/Research/Current", "prompt": "rename flag"}
    first = _hook(config, "user-prompt-submit", event, monkeypatch, capsys)
    assert first["status"] == "delivered" and first["selector_method"] == "jev"
    assert len(transport.sent_statements()) == 3
    assert _hook(config, "user-prompt-submit", event, monkeypatch, capsys)["status"] == "already_delivered"
    for source in ("compact", "resume"):
        wiped = _hook(config, "session-start", {"session_id": "s2", "source": source}, monkeypatch, capsys)
        assert wiped["status"] == "delivered"
        remaining = sorted(path.name for path in session_dir.glob("*.json"))
        assert len(remaining) == 1 and remaining[0].endswith("-session-start.json")
        # The turn's receipt was forgotten; session start re-delivered the
        # general judgments, so only the domain rule remains to select.
        replay = _hook(config, "user-prompt-submit", event, monkeypatch, capsys)
        assert replay["status"] == "delivered"
        assert transport.sent_statements() == {"Cite every failed source in the research domain."}
        assert "research domain" in replay["hookSpecificOutput"]["additionalContext"]


def test_shadow_keeps_the_deterministic_refresh_contract(tmp_path, transport, monkeypatch, capsys):
    """Shadow must deliver exactly what deterministic mode does, including
    after compact/resume, so it never resets the session's receipts."""
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    event = {"cwd": "Projects/Research/Current", "prompt": "review"}

    def session(name):
        bodies = [
            _hook(config, "session-start", {"session_id": name, "source": "startup"}, monkeypatch, capsys),
            _hook(config, "user-prompt-submit", {**event, "session_id": name}, monkeypatch, capsys),
        ]
        for source in ("compact", "resume"):
            bodies.append(_hook(config, "session-start", {"session_id": name, "source": source}, monkeypatch, capsys))
            bodies.append(_hook(config, "user-prompt-submit", {**event, "session_id": name}, monkeypatch, capsys))
        return [_strip_selector(body) for body in bodies]

    expected = session("today")
    assert [body["status"] for body in expected] == [
        "delivered", "delivered", "delivered", "already_delivered", "delivered", "already_delivered",
    ]
    _set_selector(config, selector_config("shadow"))
    assert session("shadowed") == expected
    assert transport.calls


def test_short_prompt_context_respects_caps_and_changes_the_scope(tmp_path, transport, monkeypatch, capsys):
    reply = "Earlier text. " * 300 + "Should I rename the public flag now?"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "start"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}}),
        json.dumps({"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": "sidechain"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t"}]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "yes"}}),
    ]) + "\n")
    disabled = selector_config("jev")
    config = _config(tmp_path, selector=disabled)
    _seed(config, monkeypatch, capsys)
    # Separate sessions keep session dedup from emptying the candidate pool.
    event = {"session_id": "off", "prompt": "yes", "transcript_path": str(transcript)}
    off = _hook(config, "user-prompt-submit", event, monkeypatch, capsys)
    assert transport.calls[-1]["body"]["state"] == "yes"

    enabled = selector_config("jev", short_prompt_context={"enabled": True, "below_chars": 200, "max_chars": 120})
    _set_selector(config, enabled)
    on = _hook(config, "user-prompt-submit", {**event, "session_id": "on"}, monkeypatch, capsys)
    state = transport.calls[-1]["body"]["state"]
    assert state.startswith("yes\n\nPrevious assistant reply:\n")
    excerpt = state.split("Previous assistant reply:\n", 1)[1]
    assert len(excerpt) == 120 and excerpt.endswith("Should I rename the public flag now?")
    assert "sidechain" not in state
    assert on["status"] == "delivered" and off["status"] == "delivered"
    scopes = {
        path.name.split("-domain-")[1] for path in (tmp_path / "data" / "test" / "receipts").rglob("*-domain-jev-*.json")
    }
    assert len(scopes) == 2  # the excerpt is part of the receipt scope

    long_prompt = {**event, "session_id": "long", "prompt": "p" * 250}
    _hook(config, "user-prompt-submit", long_prompt, monkeypatch, capsys)
    assert transport.calls[-1]["body"]["state"] == "p" * 250

    missing = {**event, "session_id": "missing", "prompt": "ok", "transcript_path": str(tmp_path / "absent.jsonl")}
    assert _hook(config, "user-prompt-submit", missing, monkeypatch, capsys)["status"] == "delivered"
    assert transport.calls[-1]["body"]["state"] == "ok"


def test_short_prompt_state_never_exceeds_the_limit(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant", "message": {"role": "assistant", "content": "z" * 9000},
    }) + "\n")
    selector = selector_config("jev", short_prompt_context={"enabled": True, "below_chars": 2000, "max_chars": 4000})
    state = cli._selector_state("q" * 1999, {"transcript_path": str(transcript)}, selector)
    assert len(state) <= STATE_LIMIT and state.endswith("z" * 100)


def test_retrieve_command_honors_the_selector(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"))
    _seed(config, monkeypatch, capsys)

    def run(*args):
        assert cli.main(["--config", str(config), "retrieve", "--session", "cli-session", *args]) == 0
        return json.loads(capsys.readouterr().out)

    picked = run("--prompt", "failed source")
    assert picked["selector_method"] == "jev" and picked["status"] == "delivered"
    assert run("--prompt", "failed source")["status"] == "already_delivered"
    assert len(transport.calls) == 1
    refreshed = run("--prompt", "failed source", "--refresh")
    assert refreshed["status"] == "delivered" and len(transport.calls) == 2
    deterministic = run()
    assert deterministic["selector_method"] == "deterministic"
    assert len(transport.calls) == 2



def test_already_delivered_pool_is_no_picks_without_a_request(tmp_path, transport, fake_records):
    fake_records["records"] = [
        record("d1", "Cite every failed source", section="domain", domain_id="research"),
        record("g1", "Report omissions before conclusions"),
    ]
    first = _select(tmp_path, domain="research")
    assert first.outcome == "picked"
    _commit(tmp_path, first)
    again = _select(tmp_path, state="review the sources again", domain="research")
    assert (again.method, again.outcome) == ("jev", "no_picks")
    assert again.response["payload"] == ""
    assert len(transport.calls) == 1


def test_fallback_never_reinjects_what_jev_delivered(tmp_path, transport, fake_records):
    fake_records["records"] = [
        record("d1", "Cite every failed source", section="domain", domain_id="research"),
    ]
    first = _select(tmp_path, domain="research")
    _commit(tmp_path, first)
    fake_records["records"].append(record("d2", "Name the missing dataset", section="domain", domain_id="research"))
    transport.error = OSError("synthetic outage")
    failed = _select(tmp_path, state="a different turn", domain="research")
    assert (failed.method, failed.outcome, failed.fallback_reason) == ("deterministic_fallback", "fallback", "http_error")
    assert failed.exclude_ids == frozenset({"d1"})
    shadow = _select(tmp_path, state="a third turn", domain="research", mode="shadow")
    assert shadow.exclude_ids == frozenset()  # shadow never delivered anything


def test_jev_domain_context_is_not_injected_twice(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path, selector=selector_config("jev"))
    _seed(config, monkeypatch, capsys)
    rule = "Cite every failed source in the research domain."
    event = {"session_id": "s1", "cwd": "Projects/Research/Current"}
    first = _hook(config, "user-prompt-submit", {**event, "prompt": "review the failed source"}, monkeypatch, capsys)
    assert first["selector_outcome"] == "picked"
    assert rule in first["hookSpecificOutput"]["additionalContext"]
    again = _hook(config, "user-prompt-submit", {**event, "prompt": "review sources again"}, monkeypatch, capsys)
    assert (again["selector_method"], again["selector_outcome"]) == ("jev", "no_picks")
    assert again["hookSpecificOutput"]["additionalContext"] == ""
    transport.error = OSError("synthetic outage")
    failed = _hook(config, "user-prompt-submit", {**event, "prompt": "and once more"}, monkeypatch, capsys)
    assert rule not in failed["hookSpecificOutput"]["additionalContext"]
    assert len(transport.calls) == 1


def test_retrieve_command_keeps_never_sent_records_in_every_section(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)

    def run(session, *args):
        assert cli.main(["--config", str(config), "retrieve", "--session", session, *args]) == 0
        return json.loads(capsys.readouterr().out)

    deterministic = run("plain", "--prompt", "failed source", "--domain", "research")
    assert len(deterministic["selected_ids"]) == 3
    _set_selector(config, selector_config("jev", node_types=["Rule"]))
    narrowed = run("narrowed", "--prompt", "failed source", "--domain", "research")
    assert narrowed["selector_outcome"] == "picked"
    assert set(narrowed["selected_ids"]) == set(deterministic["selected_ids"])
    assert all("Verdict" not in text for text in transport.sent_statements())


@pytest.mark.parametrize("mode", ["deterministic", "jev"])
def test_health_survives_an_unimportable_selector(tmp_path, monkeypatch, mode):
    from imprint.health import health_report

    root = tmp_path / "operator"
    config = {**copy.deepcopy(DEFAULTS), "selector": selector_config(mode)}
    monkeypatch.setitem(sys.modules, "imprint.retrieve.jev", None)
    metrics = health_report(root, ImprintStore(root / "imprint.db"), config)["metrics"]
    assert metrics["selector_mode"] == mode
    assert metrics["network_state"] == ("offline" if mode == "deterministic" else "idle")


# -- synthetic performance check ---------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="bulk synthetic store build is POSIX-sized")
def test_synthetic_user_prompt_submit_performance(tmp_path, monkeypatch, capsys):
    """~7,500 nodes (1,875 captures x Case/Verdict/Call/Evidence) with an
    instant fake Jev. Generous bound; the measured figure is printed."""
    from imprint.capture.schema import build_capture_envelope

    config = _config(tmp_path, selector=selector_config("jev"))
    root = tmp_path / "data" / "test"
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    operator = cli._operator_urn(root)
    for index in range(1875):
        store.apply_capture(build_capture_envelope(
            operator_id=operator, session_id="urn:imprint:session:00000000-0000-4000-8000-000000000001",
            node_id="primary", case_description=f"Synthetic case {index}",
            raw_operator_text=f"No, check synthetic invariant {index} before release because drift hides defects.",
            call_type="correct", capture_mechanism="explicit_cli", captured_by="perf", reason=None,
            captured_at="2026-07-14T18:00:00Z",
        ))
    assert len(store.current_nodes()) >= 7500
    monkeypatch.setattr(jev, "http_post", FakeTransport(lambda _: 0.9))
    timings = []
    for turn in range(3):
        started = time.perf_counter()
        body = _hook(config, "user-prompt-submit", {
            "session_id": "perf-session", "prompt": f"check invariant {turn} before release",
        }, monkeypatch, capsys)
        timings.append(time.perf_counter() - started)
        assert body["selector_outcome"] == "picked"
    print(f"\nsynthetic UserPromptSubmit (7,500 nodes, instant Jev): {[round(t * 1000) for t in timings]} ms")
    assert max(timings) < 30
