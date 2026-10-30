"""Read-only adapter from canonical SQLite state to retrieval records."""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from imprint.ontology.direction import PARTITION_BY_TYPE
from imprint.ontology.world import DECLARED_BUSINESS_TYPES, OBSERVED_BUSINESS_TYPES
from imprint.store import ImprintStore

from .models import (
    BUSINESS_DECLARED_PARTITION,
    BUSINESS_OBSERVED_PARTITION,
    DIRECTION_COMPARISON_PARTITION,
    JUDGMENT_PARTITION,
    SELF_MODEL_PARTITION,
    RetrievalRecord,
)


_JUDGMENT_TYPES = {"Verdict", "Principle", "Belief", "Value", "Rule", "Pattern", "IngestedItem"}
_SEMANTIC_TYPES = (
    _JUDGMENT_TYPES
    | {"SelfModelAssertion", "Observation", "Outcome"}
    | set(PARTITION_BY_TYPE)
    | set(DECLARED_BUSINESS_TYPES)
    | set(OBSERVED_BUSINESS_TYPES)
)


def _partition(node_type: str, payload: dict) -> str:
    if node_type == "SelfModelAssertion":
        return SELF_MODEL_PARTITION
    if node_type in PARTITION_BY_TYPE:
        return PARTITION_BY_TYPE[node_type]
    if node_type in DECLARED_BUSINESS_TYPES:
        return BUSINESS_DECLARED_PARTITION
    if node_type in OBSERVED_BUSINESS_TYPES or node_type in {"Observation", "Outcome"}:
        return BUSINESS_OBSERVED_PARTITION
    return JUDGMENT_PARTITION


def _path(node_type: str, payload: dict, partition: str) -> tuple[str, ...]:
    if node_type == "SelfModelAssertion":
        return tuple(str(item) for item in (
            "operator", "self_model", payload.get("function_class"),
            payload.get("subtype"), payload.get("dimension"),
        ) if item)
    if partition in {"chosen_future", "default_future", DIRECTION_COMPARISON_PARTITION}:
        return ("direction", partition, node_type)
    if partition in {BUSINESS_DECLARED_PARTITION, BUSINESS_OBSERVED_PARTITION}:
        return ("business_world", str(payload.get("evidence_mode", "unclassified")), node_type)
    return ("judgment", node_type)


def _text(node_type: str, payload: dict) -> str | None:
    for key in (
        "statement", "raw_operator_text", "description", "content", "text",
        "name", "definition", "action", "metric", "status", "referred_party",
        "candidate_move",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if node_type == "TradeOff":
        return f"Chosen: {payload.get('chosen')}; foregone: {payload.get('foregone')}; reason: {payload.get('reason')}"
    if node_type in {"Price", "Purchase", "Refund"} and payload.get("amount") is not None:
        return f"{node_type}: {payload.get('amount')} {payload.get('currency', '')}".strip()
    if node_type == "Result" and payload.get("value") is not None:
        return f"{payload.get('metric')}: {payload.get('value')} {payload.get('unit', '')}".strip()
    return None


# Statement-bearing fields per selectable judgment type. Legacy imported
# Principles carry their words in ``text``.
_STATEMENT_FIELDS = {
    "Verdict": ("raw_operator_text",),
    "Principle": ("statement", "text"),
    "Belief": ("statement",),
    "Value": ("statement",),
    "Rule": ("statement",),
    "Pattern": ("statement",),
}


def _judgment_statement(node_type: str, payload: dict) -> str | None:
    for key in _STATEMENT_FIELDS.get(node_type, ()):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _confidence(payload: dict) -> float | None:
    confidence = payload.get("confidence")
    if isinstance(confidence, dict):
        score = confidence.get("score")
        if not isinstance(score, bool) and isinstance(score, (int, float)) and 0 <= score <= 1:
            return float(score)
    return None


def _disclosure(status: str, authority: str) -> str:
    if authority == "imported_floor":
        return "approved_import_not_operator_judgment"
    return {
        "captured": "operator_captured",
        "ratified": "operator_ratified",
        "extracted": "source_extracted_not_operator_ratified",
        "inferred": "model_inference_not_operator_authority",
    }.get(status, "authority_unclassified")


class StoreRetrievalSource:
    """Expose only provenance-complete, current canonical records.

    ``provenance_complete`` is computed here rather than trusted from a model or
    serialized projection. Evidence and Case links must exist in canonical
    state before captured judgment can enter context.
    """

    def __init__(self, store: ImprintStore):
        self.store = store

    def retrieval_candidates(self, snapshot_id: str) -> Sequence[RetrievalRecord]:
        del snapshot_id  # snapshot identity is enforced by the caller/receipt.
        nodes = self.store.current_nodes()
        edges = self.store.current_edges()
        evidence_nodes = {item["node_id"] for item in nodes if item["node_type"] == "Evidence"}
        with self.store.connect() as conn:
            source_receipts = {row[0] for row in conn.execute("SELECT source_id FROM source_receipts")}
        case_nodes = {item["node_id"] for item in nodes if item["node_type"] == "Case"}
        case_summary_by_id = {
            item["node_id"]: item["payload"]["description"]
            for item in nodes
            if item["node_type"] == "Case"
            and isinstance(item["payload"].get("description"), str)
        }
        cases_by_source: dict[str, set[str]] = defaultdict(set)
        evidence_by_source: dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            if edge["edge_type"] == "verdict_about_case" and edge["target_id"] in case_nodes:
                cases_by_source[edge["source_id"]].add(edge["target_id"])
            if edge["edge_type"] == "supported_by" and edge["target_id"] in evidence_nodes:
                evidence_by_source[edge["source_id"]].add(edge["target_id"])

        records: list[RetrievalRecord] = []
        for node in nodes:
            if node["node_type"] not in _SEMANTIC_TYPES:
                continue
            payload = node["payload"]
            evidence = tuple(sorted(set(node["evidence"])))
            linked_evidence = evidence_by_source.get(node["node_id"], set())
            evidence_complete = bool(evidence) and all(
                item in evidence_nodes or item in source_receipts for item in evidence
            )
            if node["node_type"] == "Verdict":
                evidence_complete = evidence_complete and set(evidence).issubset(linked_evidence)
            cases = tuple(sorted(cases_by_source.get(node["node_id"], set())))
            status = node["provenance_status"]
            authority = node["authority_tier"]
            domain_id = payload.get("domain_id") if isinstance(payload.get("domain_id"), str) else None
            partition = _partition(node["node_type"], payload)
            section = "domain" if domain_id else (
                "core" if node["node_type"] in {"Belief", "Value", "SelfModelAssertion", "ChosenFuture", "Aim"}
                else "general"
            )
            text = _text(node["node_type"], payload)
            if not isinstance(text, str) or not text.strip():
                continue
            imported_selected = bool(payload.get("imported_selected", False))
            records.append(RetrievalRecord(
                record_id=node["node_id"],
                text=text,
                section=section,
                provenance_status=status,
                authority_tier=authority,
                evidence_ids=evidence,
                case_ids=cases,
                case_summaries=tuple(
                    # "" for a Case without a usable description keeps the
                    # tuple index-aligned with case_ids, as the model promises.
                    case_summary_by_id.get(case_id, "")
                    for case_id in cases
                ),
                source_receipt_ids=tuple(item for item in evidence if item in source_receipts),
                domain_id=domain_id,
                pinned=bool(payload.get("pinned", False)),
                recurrence_count=int(payload.get("recurrence_count", 0)),
                valid_from=node["valid_from"],
                valid_until=node["valid_to"],
                provenance_complete=evidence_complete and (
                    status != "captured" or partition == BUSINESS_DECLARED_PARTITION or bool(cases)
                ),
                imported_selected=imported_selected,
                ontology_partition=partition,
                ontology_type=node["node_type"],
                ontology_path=_path(node["node_type"], payload, partition),
                confidence=_confidence(payload),
                disclosure=_disclosure(status, authority),
                judgment_statement=_judgment_statement(node["node_type"], payload),
            ))
        return records
