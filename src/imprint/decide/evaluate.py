"""Offline evaluation of the selector on labelled cases. No network, no store.

A later comparison of question wordings needs a fixed yardstick, so this measures
today's selection path against labelled cases and saved scores:

- the real lexical prefilter (``imprint.retrieve.jev.prefilter``), as the hook runs
  it with no standing rules and no stale check, keeps ``shortlist`` candidates;
- the saved scores stand in for Jev's answers, and today's pick policy keeps those
  at or above ``score_floor``, best first, at most ``top``;
- the real renderer the prompt hook uses for Jev's picks fits them into
  ``budget_bytes``, compacting or skipping as it would.

It reports recall of the gold candidates at the shortlist size, the precision of
what would be rendered, and how many rendered candidates are not gold (unrelated
pickups). Records are built from the fixture with synthetic ids, so line sizes are
close to, not the same as, a real store's.

Cases file::

    {"cases": [{"id": "case-01", "turn": "...",
                "candidates": [{"id": "j01", "statement": "...",
                                "valid_from": "2026-01-01T00:00:00Z", "case_summary": "..."}],
                "gold": ["j01"]}]}

``valid_from`` and ``case_summary`` are optional. Scores file::

    {"scores": {"case-01": {"j01": 0.91, "j02": 0.12}}}

A shortlisted candidate with no saved score is never picked, and is counted as
``unscored``. Output holds ids and counts only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from imprint.config import DEFAULTS
from imprint.errors import ValidationError
from imprint.retrieve import _jev_response
from imprint.retrieve.jev import STATE_LIMIT, egress_text, prefilter
from imprint.retrieve.models import RetrievalRecord

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_SHORTLIST = DEFAULTS["selector"]["jev"]["prefilter_keep"]
DEFAULT_SCORE_FLOOR = DEFAULTS["selector"]["jev"]["score_floor"]
DEFAULT_TOP = DEFAULTS["selector"]["jev"]["top"]
DEFAULT_BUDGET = DEFAULTS["context_budget_bytes"]


def _read(path: Path, what: str) -> Any:
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValidationError(f"{what} file is larger than {MAX_FILE_BYTES} bytes")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{what} file is not JSON") from exc


def _identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValidationError(f"{what} must be a lowercase id")
    return value


def load_cases(path: Path) -> list[dict[str, Any]]:
    value = _read(path, "cases")
    cases = value.get("cases") if isinstance(value, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValidationError("cases file must hold a non-empty cases array")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValidationError("each case must be an object")
        case_id = _identifier(case.get("id"), "case id")
        if case_id in seen:
            raise ValidationError(f"case id {case_id} repeats")
        seen.add(case_id)
        turn = case.get("turn")
        if not isinstance(turn, str) or not turn.strip():
            raise ValidationError(f"case {case_id} needs a turn")
        candidates = case.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValidationError(f"case {case_id} needs candidates")
        ids: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValidationError(f"case {case_id} has a candidate that is not an object")
            candidate_id = _identifier(candidate.get("id"), f"case {case_id} candidate id")
            if candidate_id in ids:
                raise ValidationError(f"case {case_id} repeats candidate {candidate_id}")
            ids.add(candidate_id)
            statement = candidate.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                raise ValidationError(f"case {case_id} candidate {candidate_id} needs a statement")
            stamp = candidate.get("valid_from", "2026-01-01T00:00:00Z")
            if not isinstance(stamp, str) or _UTC.fullmatch(stamp) is None:
                raise ValidationError(f"case {case_id} candidate {candidate_id} valid_from must be a UTC time")
            summary = candidate.get("case_summary", "")
            if not isinstance(summary, str):
                raise ValidationError(f"case {case_id} candidate {candidate_id} case_summary must be text")
        gold = case.get("gold")
        if (
            not isinstance(gold, list)
            or any(not isinstance(item, str) or item not in ids for item in gold)
            or len(set(gold)) != len(gold)
        ):
            raise ValidationError(f"case {case_id} gold must list its own candidate ids once each")
    return cases


def load_scores(path: Path, cases: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    value = _read(path, "scores")
    scores = value.get("scores") if isinstance(value, dict) else None
    if not isinstance(scores, dict):
        raise ValidationError("scores file must hold a scores object")
    known = {case["id"]: {candidate["id"] for candidate in case["candidates"]} for case in cases}
    result: dict[str, dict[str, float]] = {}
    for case_id, values in scores.items():
        if case_id not in known:
            raise ValidationError(f"scores name a case the cases file does not have: {case_id}")
        if not isinstance(values, dict):
            raise ValidationError(f"scores for case {case_id} must be an object")
        result[case_id] = {}
        for candidate_id, score in values.items():
            if candidate_id not in known[case_id]:
                raise ValidationError(f"scores for case {case_id} name an unknown candidate")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
                raise ValidationError(f"score for case {case_id} candidate {candidate_id} must be 0..1")
            result[case_id][candidate_id] = float(score)
    return result


def _records(case: Mapping[str, Any]) -> dict[str, RetrievalRecord]:
    """Records the selector would see, keyed by the fixture's candidate id."""
    records = {}
    for candidate in case["candidates"]:
        tail = f"eval-{case['id']}-{candidate['id']}"
        summary = candidate.get("case_summary", "")
        records[candidate["id"]] = RetrievalRecord(
            record_id=f"urn:imprint:verdict:{tail}",
            text=candidate["statement"],
            section="general",
            provenance_status="captured",
            authority_tier="captured_judgment",
            evidence_ids=(f"urn:imprint:evidence:{tail}",),
            case_ids=(f"urn:imprint:case:{tail}",),
            case_summaries=(summary,) if summary else (),
            provenance_complete=True,
            ontology_type="Verdict",
            ontology_path=("judgment", "Verdict"),
            judgment_statement=candidate["statement"],
            valid_from=candidate.get("valid_from", "2026-01-01T00:00:00Z"),
        )
    return records


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def evaluate(
    cases: list[dict[str, Any]], scores: Mapping[str, Mapping[str, float]], *,
    shortlist: int = DEFAULT_SHORTLIST, budget: int = DEFAULT_BUDGET, score_floor: float = DEFAULT_SCORE_FLOOR,
    top: int = DEFAULT_TOP, short_lines: bool = False,
) -> dict[str, object]:
    if isinstance(shortlist, bool) or not isinstance(shortlist, int) or not 1 <= shortlist <= 10000:
        raise ValidationError("shortlist must be 1..10000")
    if isinstance(budget, bool) or not isinstance(budget, int) or not 4096 <= budget <= 131072:
        raise ValidationError("budget must be 4096..131072")
    if isinstance(score_floor, bool) or not isinstance(score_floor, (int, float)) or not 0 <= score_floor <= 1:
        raise ValidationError("score floor must be 0..1")
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 150:
        raise ValidationError("top must be 1..150")
    per_case = []
    for case in cases:
        records = _records(case)
        by_record = {record.record_id: candidate_id for candidate_id, record in records.items()}
        gold = set(case["gold"])
        sendable = [record for record in records.values() if egress_text(record)]
        kept = [by_record[record.record_id] for record in prefilter(case["turn"][:STATE_LIMIT], sendable, shortlist)]
        saved = scores.get(case["id"], {})
        scored = [(records[item], saved[item]) for item in kept if item in saved]
        scored.sort(key=lambda pick: (-pick[1], pick[0].record_id.encode("utf-8")))
        picks = [pick for pick in scored if pick[1] >= score_floor][:top]
        response = _jev_response(
            "eval", "eval", picks, "picked" if picks else "no_picks", len(records), budget,
            short_lines=short_lines,
        )
        rendered = [by_record[record_id] for record_id in response["selected_ids"]]
        rendered_gold = sum(item in gold for item in rendered)
        per_case.append({
            "id": case["id"],
            "candidates": len(records),
            "gold": len(gold),
            "shortlisted": len(kept),
            "shortlisted_gold": sum(item in gold for item in kept),
            "unscored": sum(item not in saved for item in kept),
            "picked": len(picks),
            "rendered": len(rendered),
            "rendered_gold": rendered_gold,
            "rendered_compact": int(response["selector_stages"]["delivered_compact"]),
            "unrelated_pickups": len(rendered) - rendered_gold,
            "rendered_bytes": int(response["selected_bytes"]),
        })

    def total(name: str) -> int:
        return sum(int(case[name]) for case in per_case)

    return {
        "cases": len(per_case),
        "settings": {
            "budget_bytes": budget, "lines": "short" if short_lines else "whole", "score_floor": score_floor,
            "shortlist": shortlist, "top": top,
        },
        "candidates": total("candidates"),
        "gold": total("gold"),
        "shortlisted_gold": total("shortlisted_gold"),
        "recall_at_shortlist": _ratio(total("shortlisted_gold"), total("gold")),
        "unscored": total("unscored"),
        "picked": total("picked"),
        "rendered": total("rendered"),
        "rendered_gold": total("rendered_gold"),
        "precision_rendered": _ratio(total("rendered_gold"), total("rendered")),
        "unrelated_pickups": total("unrelated_pickups"),
        "per_case": per_case,
    }


def run_selector_eval(
    cases_path: Path, scores_path: Path, *, shortlist: int = DEFAULT_SHORTLIST, budget: int = DEFAULT_BUDGET,
    score_floor: float = DEFAULT_SCORE_FLOOR, top: int = DEFAULT_TOP, short_lines: bool = False,
) -> dict[str, object]:
    cases = load_cases(cases_path)
    return evaluate(
        cases, load_scores(scores_path, cases), shortlist=shortlist, budget=budget, score_floor=score_floor,
        top=top, short_lines=short_lines,
    )


__all__ = ["evaluate", "load_cases", "load_scores", "run_selector_eval"]
