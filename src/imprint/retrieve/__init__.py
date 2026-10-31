"""Deterministic, provenance-gated context retrieval."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .engine import RetrievalEngine, _render, render_compact
from .models import (
    BUSINESS_DECLARED_PARTITION,
    BUSINESS_OBSERVED_PARTITION,
    CHOSEN_FUTURE_PARTITION,
    DEFAULT_FUTURE_PARTITION,
    DIRECTION_COMPARISON_PARTITION,
    JUDGMENT_PARTITION,
    ONTOLOGY_PARTITIONS,
    SELF_MODEL_PARTITION,
    AuthorityMode,
    RetrievalConfig,
    RetrievalRecord,
    RetrievalResult,
)
from .receipts import DeliveryReceipts
from .store_source import StoreRetrievalSource
from .tokenizer import TOKENIZER_VERSION


def _snapshot_id(store) -> str:
    raw = json.dumps(store.snapshot(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _session_ref(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


class _CachedSource:
    """Read the store projection at most once per retrieval call."""

    def __init__(self, source):
        self._source = source
        self._records: tuple[RetrievalRecord, ...] | None = None

    def retrieval_candidates(self, snapshot_id: str) -> tuple[RetrievalRecord, ...]:
        if self._records is None:
            self._records = tuple(self._source.retrieval_candidates(snapshot_id))
        return self._records


@dataclass(frozen=True)
class RetrievalContext:
    """One snapshot identity and one lazy candidate read, shared by a single
    call's selector attempt and its deterministic fallback."""

    snapshot_id: str
    source: _CachedSource

    @classmethod
    def read(cls, store) -> "RetrievalContext":
        return cls(_snapshot_id(store), _CachedSource(StoreRetrievalSource(store)))


def retrieve_payload(store, *, root: Path, session_id: str, prompt: str = "", explicit_domain: str | None = None,
                     budget: int = 32 * 1024, refresh: bool = False,
                     domain_only: bool = False,
                     context: RetrievalContext | None = None,
                     exclude_ids: frozenset[str] = frozenset()) -> dict[str, object]:
    """Build one bounded payload per session/snapshot with an atomic receipt.

    ``exclude_ids`` drops records the selector already delivered into this
    session, so a selector fallback never re-injects them.
    """
    context = context or RetrievalContext.read(store)
    snapshot_id = context.snapshot_id
    safe_session = _session_ref(session_id)
    receipts = DeliveryReceipts(Path(root) / "receipts")
    source = context.source
    if domain_only or exclude_ids:
        class DomainOnlySource:
            def retrieval_candidates(self, requested_snapshot_id):
                return tuple(
                    item for item in source.retrieval_candidates(requested_snapshot_id)
                    if (not domain_only or item.section == "domain") and item.record_id not in exclude_ids
                )
        retrieval_source = DomainOnlySource()
    else:
        retrieval_source = source
    # Validate every retrieval invariant before consuming the once-delivery latch.
    engine = RetrievalEngine(
        retrieval_source,
        RetrievalConfig(total_budget_bytes=budget, allow_higher_budget=budget > 32 * 1024),
    )
    if domain_only and not explicit_domain:
        raise ValueError("domain_only retrieval requires an explicit selected domain")
    receipt_domain = explicit_domain if domain_only else None
    if not refresh:
        pending, delivered = receipts._paths(safe_session, snapshot_id, receipt_domain)
        if delivered.exists():
            return {"status": "already_delivered", "snapshot_id": snapshot_id, "payload": "", "selected_ids": []}
        if pending.exists():
            cached = receipts._decode_prepared(pending)
            return cached
    result = engine.retrieve(snapshot_id=snapshot_id, query=prompt, selected_domain=explicit_domain)
    response: dict[str, object] = {
        "status": "delivered",
        "snapshot_id": snapshot_id,
        "payload": result.payload.decode("utf-8"),
        "selected_ids": list(result.selected_ids),
        "selected_bytes": result.selected_bytes,
        "budget_bytes": result.budget_bytes,
        "eligible_count": result.eligible_count,
        "omitted_count": result.omitted_count,
        "section_bytes": result.section_bytes,
        "tokenizer_version": result.tokenizer_version,
        "authority_mode": result.authority_mode,
        "requested_partitions": list(result.requested_partitions),
        "selected_by_partition": {
            partition: list(ids) for partition, ids in result.selected_by_partition.items()
        },
    }
    if refresh:
        return response
    state, cached = receipts.prepare_delivery(
        safe_session, snapshot_id, receipt_domain, response,
    )
    if state == "delivered":
        return {"status": "already_delivered", "snapshot_id": snapshot_id, "payload": "", "selected_ids": []}
    assert cached is not None
    return cached


def commit_payload_delivery(
    *, root: Path, session_id: str, snapshot_id: str, domain_id: str | None = None,
) -> bool:
    """Commit a prepared delivery only after its caller flushed the payload."""
    return DeliveryReceipts(Path(root) / "receipts").commit_delivery(
        _session_ref(session_id), snapshot_id, domain_id,
    )


def reset_payload_delivery(*, root: Path, session_id: str) -> None:
    """Forget a session's delivery latches after its host context was wiped."""
    DeliveryReceipts(Path(root) / "receipts").reset_session(_session_ref(session_id))


@dataclass(frozen=True)
class SelectorDecision:
    """What the optional relevance selector decided for one turn.

    ``response`` is set only when Jev owns the delivery. Otherwise the caller
    delivers exactly its deterministic behavior, reusing ``context`` so the
    store is not read twice, minus ``exclude_ids``: records Jev already
    delivered into this session, which today's once-per-session latch would
    never repeat.
    """

    method: str
    outcome: str | None = None
    fallback_reason: str | None = None
    response: dict[str, object] | None = None
    context: RetrievalContext | None = None
    exclude_ids: frozenset[str] = frozenset()

    def fields(self) -> dict[str, object]:
        """Content-free diagnostics for hook and CLI output."""
        value: dict[str, object] = {"selector_method": self.method}
        if self.outcome is not None:
            value["selector_outcome"] = self.outcome
        if self.fallback_reason is not None:
            value["fallback_reason"] = self.fallback_reason
        return value


def jev_receipt_scope(state: str, domain: str | None, domain_only: bool = False) -> str:
    """Receipt scope for one exact selector turn; a valid receipt domain id.

    The receipt file is ``<snapshot>-domain-jev-<24 hex>.json``.
    """
    contract = json.dumps([state, domain, domain_only], ensure_ascii=False, separators=(",", ":"))
    return "jev-" + hashlib.sha256(contract.encode("utf-8")).hexdigest()[:24]


def _already_delivered(snapshot_id: str) -> dict[str, object]:
    return {"status": "already_delivered", "snapshot_id": snapshot_id, "payload": "", "selected_ids": []}


class _FixedSource:
    """A retrieval source over records one call has already filtered."""

    def __init__(self, records: Sequence[RetrievalRecord]):
        self._records = tuple(records)

    def retrieval_candidates(self, snapshot_id: str) -> tuple[RetrievalRecord, ...]:
        return self._records


# When the picks cannot all fit, one may still take this fraction of the budget in
# full. Past it, a single large record would crowd out the others Jev ranked below it.
_FULL_RENDER_SHARE = 4


def _jev_response(
    snapshot_id: str, scope: str, picks: Sequence[tuple[RetrievalRecord, float]], outcome: str,
    eligible_count: int, budget: int, local: RetrievalResult | None = None,
    retried: frozenset[str] = frozenset(), reserved: frozenset[str] = frozenset(),
    withheld: int = 0,
) -> dict[str, object]:
    """Render ``local`` (the matched domain's never-sent records, ranked
    deterministically) and then picks in score order, never exceeding the
    selector budget.

    Picks that all fit stay whole. Otherwise a pick too large to share the budget
    is delivered compact, and so is any pick the budget has run out for. A compact
    pick that could fit on its own goes in ``retry_ids``, so the session is not
    latched against its whole form. ``retried`` holds picks already cut that way:
    they are no longer held back for size, or two large picks Jev keeps choosing
    would be cut on every turn. Only a pick that can never fit is latched compact.
    ``reserved`` picks are standing rules, which are marked on their lines, and
    ``withheld`` counts stale records kept from this turn's request.

    Picks that cannot be delivered at all report ``oversized_picks``, or
    ``crowded_picks`` when one would have fit without the local records, because
    neither is the same as nothing being relevant.
    """
    rendered: list[bytes] = []
    selected_ids: list[str] = []
    retry_ids: list[str] = []
    section_bytes = {"core": 0, "general": 0, "domain": 0}
    selected_by_partition: dict[str, list[str]] = {}
    total = 0
    if local is not None:
        rendered.append(local.payload)
        total += len(local.payload)
        selected_ids.extend(local.selected_ids)
        for section, size in local.section_bytes.items():
            section_bytes[section] += size
        for partition, ids in local.selected_by_partition.items():
            selected_by_partition.setdefault(partition, []).extend(ids)
    local_bytes = total
    selections = [
        {"mode": "jev", "rank": rank, "score": round(score, 4), **({"reserved": True} if record.record_id in reserved else {})}
        for rank, (record, score) in enumerate(picks, 1)
    ]
    whole_lines = [_render(record, selector=selection) for (record, _), selection in zip(picks, selections)]
    all_fit = total + sum(len(line) for line in whole_lines) <= budget
    delivered_picks = compact_picks = 0
    crowded = False
    for (record, _), selection, whole in zip(picks, selections, whole_lines):
        too_large = len(whole) > budget // _FULL_RENDER_SHARE and record.record_id not in retried
        compact = (not all_fit and too_large) or total + len(whole) > budget
        line = render_compact(record, selector=selection) if compact else whole
        if total + len(line) > budget:
            crowded |= len(line) <= budget
            continue
        compact_picks += compact
        total += len(line)
        delivered_picks += 1
        rendered.append(line)
        selected_ids.append(record.record_id)
        if compact and len(whole) <= budget:
            retry_ids.append(record.record_id)
        section_bytes[record.section] += len(line)
        selected_by_partition.setdefault(record.ontology_partition, []).append(record.record_id)
    if outcome == "picked" and not delivered_picks:
        outcome = "crowded_picks" if crowded else "oversized_picks"
    payload = b"".join(rendered)
    if len(payload) > budget:  # defensive invariant
        raise RuntimeError("selector budget invariant violated")
    return {
        "status": "delivered",
        "snapshot_id": snapshot_id,
        "payload": payload.decode("utf-8"),
        "selected_ids": selected_ids,
        "retry_ids": retry_ids,
        "selected_bytes": len(payload),
        "budget_bytes": budget,
        "eligible_count": eligible_count,
        "omitted_count": eligible_count - len(selected_ids),
        "section_bytes": section_bytes,
        "tokenizer_version": TOKENIZER_VERSION,
        "authority_mode": "authoritative",
        "requested_partitions": [],
        "selected_by_partition": selected_by_partition,
        "receipt_scope": scope,
        "selector_method": "jev",
        "selector_outcome": outcome,
        "selector_stages": {
            "eligible": eligible_count,
            "picked": len(picks),
            "delivered": delivered_picks,
            "delivered_compact": compact_picks,
            "unfit": len(picks) - delivered_picks,
            "local_records": len(local.selected_ids) if local is not None else 0,
            "local_bytes": local_bytes,
            **({"reserved": len(reserved)} if reserved else {}),
            **({"stale_withheld": withheld} if withheld else {}),
        },
    }


def select_prompt_payload(
    store, *, root: Path, session_id: str, state: str, explicit_domain: str | None,
    budget: int, selector: Mapping[str, Any], mode: str, refresh: bool = False,
    deadline: float | None = None, local_sections: frozenset[str] = frozenset({"domain"}),
) -> SelectorDecision:
    """Run the optional relevance selector for one turn. Never raises.

    ``mode`` is ``jev`` (Jev owns delivery unless it falls back) or
    ``shadow`` (Jev is called and logged; the caller always delivers its
    deterministic behavior). Eligibility is the deterministic engine's, then
    narrowed to selectable judgment types, minus excluded domains and records
    already delivered in this session under any snapshot. Those filters bound
    only what is sent: never-sent records in ``local_sections`` (the hook's
    matched domain; every section for ``imprint retrieve``) are delivered
    locally ahead of the picks, as deterministic retrieval would deliver them.
    When every sendable record was already delivered, the turn is ``no_picks``
    without a request. Records configured out as stale are delivered in neither way.

    ``deadline`` (a ``time.monotonic()`` value) caps the configured
    ``timeout_seconds`` for callers with a hard wall-clock limit of their own.
    """
    shadow = mode == "shadow"
    failed_method = "shadow" if shadow else "deterministic_fallback"
    try:
        from .jev import (
            STATE_LIMIT, JevSelector, JevSettings, SelectionResult, decision_entry,
            egress_text, log_decision, reads_as_feedback, record_transfer, rule_sentences,
        )
    except Exception:
        return SelectorDecision(failed_method, "fallback", "exception")
    context: RetrievalContext | None = None
    scope: str | None = None
    result: SelectionResult | None = None
    delivered_ids: set[str] = set()
    retried_ids: set[str] = set()
    session_ref = _session_ref(session_id)

    def already_sent() -> frozenset[str]:
        # Shadow never delivers, so only jev mode has picks to keep out.
        return frozenset() if shadow else frozenset(delivered_ids)
    try:
        jev = selector["jev"]
        log = bool(jev.get("log_decisions", True))
    except Exception:
        jev, log = None, False

    def record(
        value: SelectionResult, stages: Mapping[str, int] | None = None, delivery: str | None = None,
    ) -> None:
        """Exactly one content-free decision line per call, written once its
        delivery is settled, plus the transfer marker whenever bytes left.

        ``value.outcome`` stays Jev's own result, which is what transfer health
        counts as a success. ``delivery`` is what became of it in this turn.
        """
        try:
            if log:
                log_decision(root, decision_entry(
                    value, mode=mode, session_ref=session_ref, receipt_scope=scope, stages=stages,
                    delivery_outcome=delivery,
                ))
            if value.request_sent:
                record_transfer(root, success=value.outcome in {"picked", "no_picks"})
        except Exception:
            pass

    try:
        if mode not in {"jev", "shadow"} or not isinstance(jev, Mapping):
            raise ValueError("selector is not enabled")
        settings = JevSettings.from_config(jev)
        state = state[:STATE_LIMIT]
        selector_budget = int(jev["budget_bytes"] or budget)
        context = RetrievalContext.read(store)
        receipts = DeliveryReceipts(Path(root) / "receipts")
        engine = RetrievalEngine(
            context.source,
            RetrievalConfig(
                total_budget_bytes=selector_budget,
                allow_higher_budget=selector_budget > 32 * 1024,
            ),
        )
        if not shadow:
            scope = jev_receipt_scope(state, explicit_domain)
            if not refresh:
                pending, delivered = receipts._paths(session_ref, context.snapshot_id, scope)
                if delivered.exists():
                    return SelectorDecision(
                        "jev", "already_delivered",
                        response=_already_delivered(context.snapshot_id), context=context,
                    )
                if pending.exists():
                    cached = receipts._decode_prepared(pending)
                    return SelectorDecision(
                        "jev", str(cached.get("selector_outcome") or "picked"),
                        response=cached, context=context,
                    )
        if not refresh:
            delivered_ids, retried_ids = receipts.delivery_state(session_ref)
        node_types = set(jev["node_types"])
        excluded_domains = set(jev["exclude_domains"])
        candidates = engine.eligible_candidates(
            snapshot_id=context.snapshot_id, selected_domain=explicit_domain,
        )
        accept = reads_as_feedback if jev["drop_stale_records"] else None
        sendable = {
            item.record_id for item in candidates
            if item.ontology_partition == JUDGMENT_PARTITION
            and item.ontology_type in node_types
            and item.domain_id not in excluded_domains
            and egress_text(item) is not None
        }
        pool = [item for item in candidates if item.record_id in sendable and item.record_id not in delivered_ids]
        standing: frozenset[str] = frozenset()
        if jev["standing_rules"]["enabled"]:
            rules = sorted((item for item in pool if rule_sentences(item)), key=lambda item: item.valid_from, reverse=True)
            standing = frozenset(item.record_id for item in rules[: jev["standing_rules"]["keep"]])
        if deadline is not None:
            settings = replace(settings, timeout_seconds=min(settings.timeout_seconds, deadline - time.monotonic()))
        if sendable and not pool:
            # Everything relevant is already in this session's context.
            result = SelectionResult(outcome="no_picks")
        else:
            result = JevSelector(settings).select(state, pool, standing=standing, accept=accept)
        if result.outcome == "fallback":
            record(result)
            return SelectorDecision(
                failed_method, "fallback", result.fallback_reason, context=context, exclude_ids=already_sent(),
            )
        if shadow:
            record(result)
            return SelectorDecision("shadow", result.outcome, context=context)
        assert scope is not None
        # Records Imprint never sends (an excluded domain, an unselected type,
        # no statement) keep their deterministic delivery.
        local_records = [
            item for item in candidates
            if item.section in local_sections and item.record_id not in sendable
            and item.record_id not in delivered_ids and (accept is None or accept(item))
        ]
        local = RetrievalEngine(_FixedSource(local_records), engine.config).retrieve(
            snapshot_id=context.snapshot_id, query=state, selected_domain=explicit_domain,
        ) if local_records else None
        response = _jev_response(
            context.snapshot_id, scope, result.picks, result.outcome,
            len(pool) + len(local_records), selector_budget, local, frozenset(retried_ids),
            result.reserved, result.withheld,
        )
        outcome = str(response["selector_outcome"])
        stages = response["selector_stages"]
        if refresh:
            record(result, stages, outcome)
            return SelectorDecision("jev", outcome, response=response, context=context)
        status, cached = receipts.prepare_delivery(session_ref, context.snapshot_id, scope, response)
        if status == "delivered":
            record(result, stages, "already_delivered")
            return SelectorDecision(
                "jev", "already_delivered",
                response=_already_delivered(context.snapshot_id), context=context,
            )
        assert cached is not None
        record(result, stages, outcome)
        return SelectorDecision("jev", outcome, response=cached, context=context)
    except Exception:
        # A failure after Jev answered is still one fallback line: the turn
        # fell back, even though the request itself succeeded.
        record(SelectionResult(fallback_reason="exception") if result is None else replace(
            result, picks=(), outcome="fallback", fallback_reason="exception",
        ))
        return SelectorDecision(failed_method, "fallback", "exception", context=context, exclude_ids=already_sent())


__all__ = [
    "BUSINESS_DECLARED_PARTITION",
    "BUSINESS_OBSERVED_PARTITION",
    "CHOSEN_FUTURE_PARTITION",
    "commit_payload_delivery",
    "DEFAULT_FUTURE_PARTITION",
    "AuthorityMode",
    "DeliveryReceipts",
    "DIRECTION_COMPARISON_PARTITION",
    "JUDGMENT_PARTITION",
    "jev_receipt_scope",
    "ONTOLOGY_PARTITIONS",
    "reset_payload_delivery",
    "RetrievalConfig",
    "RetrievalContext",
    "RetrievalEngine",
    "RetrievalRecord",
    "RetrievalResult",
    "SELF_MODEL_PARTITION",
    "select_prompt_payload",
    "SelectorDecision",
    "StoreRetrievalSource",
    "retrieve_payload",
]
