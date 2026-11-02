"""Eligibility, stable ranking, and byte-exact context compilation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Sequence

from imprint.capture.detector import (
    closing_sentence,
    feedback_sentences,
    operator_words,
    stored_closing_sentence,
    stored_words,
)

from .models import (
    CHOSEN_FUTURE_PARTITION,
    DEFAULT_FUTURE_PARTITION,
    JUDGMENT_PARTITION,
    ONTOLOGY_PARTITIONS,
    AuthorityMode,
    RetrievalConfig,
    RetrievalRecord,
    RetrievalResult,
    RetrievalSource,
)
from .tokenizer import TOKENIZER_VERSION, lexical_score

DEFAULT_BUDGET = 32 * 1024
MAX_EXPLICIT_BUDGET = 128 * 1024
_BASE_WEIGHTS = {"core": 1, "general": 2, "domain": 1}
_DIRECT_AUTHORITY = {"captured": 0, "ratified": 0, "extracted": 1}
_TIER_ORDER = {
    "captured_judgment": 0,
    "ratified_knowledge": 0,
    "imported_floor": 2,
}
_ANALYTICAL_TIERS = {"inferred_candidate", "observed_candidate"}


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _recency_bucket(value: str) -> int:
    # Day buckets are deterministic and avoid wall-clock-dependent ranking.
    return int(_parse_time(value).timestamp() // 86400)


def eligible(
    record: RetrievalRecord,
    selected_domain: str | None,
    authority_mode: AuthorityMode = "authoritative",
) -> bool:
    if not record.current or record.rejected or record.tombstoned or record.valid_until is not None:
        return False
    if not record.provenance_complete or not record.evidence_ids:
        return False
    if authority_mode not in {"authoritative", "analytical"}:
        return False
    authoritative = authority_mode == "authoritative"
    allowed_statuses = {"captured", "extracted", "ratified"}
    if not authoritative:
        allowed_statuses.add("inferred")
    if record.provenance_status not in allowed_statuses:
        return False
    allowed_tiers = {
        "captured_judgment",
        "ratified_knowledge",
        "imported_floor",
    }
    if not authoritative:
        allowed_tiers.update(_ANALYTICAL_TIERS)
    if record.authority_tier not in allowed_tiers:
        return False
    if record.authority_tier == "captured_judgment" and record.provenance_status != "captured":
        return False
    if (
        record.provenance_status == "captured"
        and record.ontology_partition != "business_declared"
        and not record.case_ids
    ):
        return False
    if record.authority_tier == "ratified_knowledge" and record.provenance_status != "ratified":
        return False
    if record.authority_tier == "imported_floor" and not record.imported_selected:
        return False
    if record.provenance_status == "inferred" and record.authority_tier != "inferred_candidate":
        return False
    if record.authority_tier == "observed_candidate" and record.provenance_status != "extracted":
        return False
    if record.ontology_partition == "self_model" and authoritative:
        if record.ontology_type != "SelfModelAssertion" or record.provenance_status != "ratified":
            return False
    if record.ontology_partition == CHOSEN_FUTURE_PARTITION:
        if record.provenance_status != "ratified":
            return False
    if record.ontology_partition == DEFAULT_FUTURE_PARTITION:
        if authoritative or record.provenance_status != "inferred":
            return False
    if record.section == "domain" and (selected_domain is None or record.domain_id != selected_domain):
        return False
    if record.domain_id is not None and record.section != "domain":
        # Domain-bound material cannot escape through a general section label.
        return False
    return True


def _rank_key(record: RetrievalRecord, query: str, selected_domain: str | None, version: str) -> tuple:
    exact_domain = selected_domain is not None and record.domain_id == selected_domain
    return (
        -int(record.pinned),
        -int(exact_domain),
        -lexical_score(query, record.text, version=version),
        _DIRECT_AUTHORITY.get(record.provenance_status, 9),
        _TIER_ORDER.get(record.authority_tier, 9),
        -max(0, record.recurrence_count),
        -_recency_bucket(record.valid_from),
        record.record_id.encode("utf-8"),
    )


COMPACT_TEXT_BYTES = 400
SHORT_LINE_DETAIL = "imprint history <record_id>"
# What ``render_short(lookup=True)`` adds to a line.
SHORT_LINE_LOOKUP_BYTES = len(f',"detail":"{SHORT_LINE_DETAIL}"')


def _section_limits(total: int) -> dict[str, int]:
    # Preserve the frozen 1:2:1 split for lower and explicitly higher budgets.
    unit, remainder = divmod(total, 4)
    limits = {"core": unit, "general": unit * 2, "domain": unit}
    # Deterministic remainder allocation follows the downward flow order.
    for name in ("core", "general", "domain")[:remainder]:
        limits[name] += 1
    return limits


def _authority(record: RetrievalRecord) -> str:
    return "imported_floor" if record.authority_tier == "imported_floor" else record.provenance_status


def _line(value: dict[str, object], selector: dict[str, object] | None) -> bytes:
    if selector is not None:
        # Additive: deterministic output never carries this key.
        value["selector"] = selector
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _render(record: RetrievalRecord, *, selector: dict[str, object] | None = None) -> bytes:
    value: dict[str, object] = {
        "authority": _authority(record),
        "case_ids": list(record.case_ids),
        "case_summaries": list(record.case_summaries),
        "evidence_ids": list(record.evidence_ids),
        "ontology": {
            "confidence": record.confidence,
            "disclosure": record.disclosure,
            "partition": record.ontology_partition,
            "path": list(record.ontology_path),
            "type": record.ontology_type,
        },
        "record_id": record.record_id,
        "source_receipt_ids": list(record.source_receipt_ids),
        "text": record.text,
    }
    return _line(value, selector)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut(text: str, limit: int = COMPACT_TEXT_BYTES) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _join_sentences(sentences: Sequence[str]) -> str:
    """Join sentences in order within the limit, nearest the end first.

    The end is where a message overrules what came before it, so the last sentence
    is always there and earlier ones fill backward from it. A last sentence too
    long for half the limit leaves that half to the others and is cut to fit.
    """
    room = COMPACT_TEXT_BYTES - min(_size(sentences[-1]), COMPACT_TEXT_BYTES // 2)
    kept: list[str] = []
    for sentence in reversed(sentences[:-1]):
        cost = _size(sentence) + 1
        if cost <= room:
            kept.append(sentence)
            room -= cost
    used = sum(_size(sentence) + 1 for sentence in kept)
    return "\n".join((*reversed(kept), _cut(sentences[-1], COMPACT_TEXT_BYTES - used)))


def _opening_with_close(text: str) -> str:
    """Return the opening of the operator's words, and how they ended if it stops short.

    A record that is nothing but quoted text keeps its own text, so a line is
    never empty: an empty line would count as delivered and say nothing.
    """
    opening = stored_words(operator_words(text))
    head = _cut(opening or text)
    closing = closing_sentence(text)
    if closing is None or closing in head or closing == opening or _size(closing) > COMPACT_TEXT_BYTES // 2:
        return head
    return f"{_cut(opening, COMPACT_TEXT_BYTES - _size(closing) - 1)}\n{closing}"


def _compact_text(text: str, rules: Sequence[str] = ()) -> tuple[str, str]:
    """Return what a compact line says, and what that text is."""
    if _size(text) <= COMPACT_TEXT_BYTES:
        return text, "whole"
    if rules:
        return _cut("\n".join(rules)), "rule_sentences"
    sentences = feedback_sentences(text)
    if sentences:
        closing = closing_sentence(text)
        if closing is not None and closing != sentences[-1]:
            sentences.append(closing)
        return _join_sentences(sentences), "feedback_sentences"
    return _opening_with_close(text), "opening"


def render_compact(
    record: RetrievalRecord, *, selector: dict[str, object] | None = None, rules: Sequence[str] = (),
) -> bytes:
    """Render the point of a record with its authority and source references.

    This shapes one delivery only; the canonical record is untouched. The text
    is the record's own words, never a rewrite: all of it when it fits, else every
    sentence that reads as feedback and the last sentence, else the opening of what
    the operator said and how it ended, cut at a byte limit. ``detail`` is how to
    read the whole record locally. ``rules`` are the sentences a standing rule was
    delivered for, which come first because they are why it was delivered.
    """
    text, basis = _compact_text(record.text, rules)
    return _line({
        "authority": _authority(record),
        "case_ids": list(record.case_ids),
        "compact": True,
        "detail": f"imprint history {record.record_id}",
        "evidence_ids": list(record.evidence_ids),
        "record_id": record.record_id,
        "source_receipt_ids": list(record.source_receipt_ids),
        "text": text,
        "text_basis": basis,
        "text_complete": text == record.text,
    }, selector)


def whole_text_fits(record: RetrievalRecord) -> bool:
    """Whether a compact or short line can say all of a record's text."""
    return _size(record.text) <= COMPACT_TEXT_BYTES


def _rule_with_close(rules: Sequence[str], closing: str | None) -> str:
    """Return a standing rule's sentences and how its record ended.

    The end is where a message overrules what came before it, and a reserved mark
    says the line stands, so a rule never goes out without it. The closing takes at
    most half the limit, so the rule always has the rest.
    """
    said = "\n".join(rules)
    if closing is None or closing in said:
        return _cut(said)
    close = _cut(closing, COMPACT_TEXT_BYTES // 2)
    return f"{_cut(said, COMPACT_TEXT_BYTES - _size(close) - 1)}\n{close}"


def _stored_opening(text: str) -> str:
    """Return the start of stored text as it stands, and how it ended if it stops short."""
    stored = text.strip()
    head = _cut(stored)
    closing = stored_closing_sentence(stored)
    if closing is None or closing in head or _size(closing) > COMPACT_TEXT_BYTES // 2:
        return head
    return f"{_cut(stored, COMPACT_TEXT_BYTES - _size(closing) - 1)}\n{closing}"


def _short_text(record: RetrievalRecord, rules: Sequence[str]) -> tuple[str, str]:
    """Return what a short line says, and what that text is.

    The compact rules read an operator's message, where a quote is someone else's
    words. A short line also carries documents and imports, where a quote can be the
    rule itself, so only a captured record is read that way.
    """
    if whole_text_fits(record):
        return record.text, "whole"
    captured = record.provenance_status == "captured"
    if rules:
        ending = closing_sentence if captured else stored_closing_sentence
        return _rule_with_close(rules, ending(record.text)), "rule_sentences"
    if captured:
        return _compact_text(record.text)
    return _stored_opening(record.text), "stored_text"


def render_short(
    record: RetrievalRecord, *, selector: dict[str, object] | None = None, rules: Sequence[str] = (),
    lookup: bool = False,
) -> bytes:
    """Render a record's words with only what a reader needs to trust and find it.

    This shapes one delivery only. The text is always the record's own words:
    ``_short_text`` says how a long one is cut. ``authority`` says how far to trust it
    and ``record_id`` is what ``imprint history`` takes to return the rest, so neither
    is ever left off. ``partition`` appears on anything but a judgment, because a
    declared claim and an observed result can read alike under the same authority.
    ``text_basis`` and ``text_complete`` appear only on text that was cut, so their
    absence means the text is all there. ``lookup`` adds ``detail``, the form of that
    command, which one line of a payload can say for all of them.
    """
    text, basis = _short_text(record, rules)
    value: dict[str, object] = {"authority": _authority(record), "record_id": record.record_id, "text": text}
    if record.ontology_partition != JUDGMENT_PARTITION:
        value["partition"] = record.ontology_partition
    if lookup:
        value["detail"] = SHORT_LINE_DETAIL
    if not whole_text_fits(record):
        value.update(text_basis=basis, text_complete=False)
    return _line(value, selector)


class RetrievalEngine:
    def __init__(self, source: RetrievalSource, config: RetrievalConfig | None = None):
        self.source = source
        self.config = config or RetrievalConfig()
        self._validate_config()

    def _validate_config(self) -> None:
        budget = self.config.total_budget_bytes
        if budget <= 0:
            raise ValueError("retrieval budget must be positive")
        if budget > DEFAULT_BUDGET and not self.config.allow_higher_budget:
            raise ValueError("higher retrieval budget requires explicit opt-in")
        if budget > MAX_EXPLICIT_BUDGET:
            raise ValueError("retrieval budget exceeds explicit hard bound")
        if self.config.tokenizer_version != TOKENIZER_VERSION:
            raise ValueError("unsupported tokenizer version")
        if self.config.authority_mode not in {"authoritative", "analytical"}:
            raise ValueError("unsupported retrieval authority mode")

    @staticmethod
    def _partitions(
        requested: Sequence[str] | None,
        authority_mode: AuthorityMode,
    ) -> tuple[str, ...]:
        if requested is None:
            if authority_mode == "analytical":
                raise ValueError("analytical retrieval requires explicit ontology partitions")
            return ()
        if isinstance(requested, str):
            raise ValueError("ontology_partitions must be a sequence of partition names")
        partitions = tuple(dict.fromkeys(requested))
        if authority_mode == "analytical" and not partitions:
            raise ValueError("analytical retrieval requires explicit ontology partitions")
        unknown = set(partitions) - ONTOLOGY_PARTITIONS
        if unknown:
            raise ValueError(f"unsupported ontology partitions: {sorted(unknown)}")
        if CHOSEN_FUTURE_PARTITION in partitions and DEFAULT_FUTURE_PARTITION in partitions:
            raise ValueError(
                "chosen_future and default_future require separate retrieval calls"
            )
        return partitions

    def _mode_and_partitions(
        self,
        ontology_partitions: Sequence[str] | None,
        authority_mode: AuthorityMode | None,
    ) -> tuple[AuthorityMode, tuple[str, ...]]:
        mode = authority_mode or self.config.authority_mode
        if mode not in {"authoritative", "analytical"}:
            raise ValueError("unsupported retrieval authority mode")
        return mode, self._partitions(ontology_partitions, mode)

    def _eligible(
        self,
        snapshot_id: str,
        selected_domain: str | None,
        partitions: tuple[str, ...],
        mode: AuthorityMode,
    ) -> list[RetrievalRecord]:
        return [
            item for item in self.source.retrieval_candidates(snapshot_id)
            if eligible(item, selected_domain, mode)
            and (not partitions or item.ontology_partition in partitions)
        ]

    def eligible_candidates(
        self,
        *,
        snapshot_id: str,
        selected_domain: str | None = None,
        ontology_partitions: Sequence[str] | None = None,
        authority_mode: AuthorityMode | None = None,
    ) -> list[RetrievalRecord]:
        """Return the records retrieve() may consider, unranked.

        This is the single eligibility gate (authority, provenance, the
        chosen_future/default_future partition rules, and domain binding) for
        any selector that ranks candidates differently.
        """
        mode, partitions = self._mode_and_partitions(ontology_partitions, authority_mode)
        return self._eligible(snapshot_id, selected_domain, partitions, mode)

    def retrieve(
        self,
        *,
        snapshot_id: str,
        query: str = "",
        selected_domain: str | None = None,
        ontology_partitions: Sequence[str] | None = None,
        authority_mode: AuthorityMode | None = None,
    ) -> RetrievalResult:
        mode, partitions = self._mode_and_partitions(ontology_partitions, authority_mode)
        candidates = self._eligible(snapshot_id, selected_domain, partitions, mode)
        candidates.sort(
            key=lambda item: _rank_key(
                item, query, selected_domain, self.config.tokenizer_version
            )
        )
        by_section: dict[str, list[RetrievalRecord]] = {"core": [], "general": [], "domain": []}
        for item in candidates:
            by_section[item.section].append(item)

        limits = _section_limits(self.config.total_budget_bytes)
        carry = 0
        selected: list[tuple[str, bytes]] = []
        retry_ids: list[str] = []
        section_bytes: dict[str, int] = {"core": 0, "general": 0, "domain": 0}
        short = self.config.short_lines and mode == "authoritative"
        for section in ("core", "general", "domain"):
            available = limits[section] + carry
            full = False
            for item in by_section[section]:
                cut = short and not whole_text_fits(item)
                if cut and full:
                    # Cutting reads all of a long record and this scan meets every
                    # eligible one, so a section that has refused a line stops paying.
                    continue
                rendered = render_short(item, lookup=not selected) if short else _render(item)
                if len(rendered) <= available - section_bytes[section]:
                    selected.append((item.record_id, rendered))
                    section_bytes[section] += len(rendered)
                    if cut:
                        # Every cut record, because this budget is not the one a
                        # later pick spends: that route settles what can arrive whole.
                        retry_ids.append(item.record_id)
                else:
                    full = True
            carry = available - section_bytes[section]

        payload = b"".join(value for _, value in selected)
        if len(payload) > self.config.total_budget_bytes:  # defensive invariant
            raise RuntimeError("retrieval budget invariant violated")
        selected_ids = tuple(record_id for record_id, _ in selected)
        selected_set = set(selected_ids)
        selected_by_partition: dict[str, tuple[str, ...]] = {}
        for item in candidates:
            if item.record_id in selected_set:
                selected_by_partition.setdefault(item.ontology_partition, tuple())
                selected_by_partition[item.ontology_partition] += (item.record_id,)
        return RetrievalResult(
            payload=payload,
            selected_ids=selected_ids,
            eligible_count=len(candidates),
            omitted_count=len(candidates) - len(selected_ids),
            selected_bytes=len(payload),
            budget_bytes=self.config.total_budget_bytes,
            section_bytes=section_bytes,
            tokenizer_version=self.config.tokenizer_version,
            authority_mode=mode,
            requested_partitions=partitions,
            selected_by_partition=selected_by_partition,
            retry_ids=tuple(retry_ids),
        )
