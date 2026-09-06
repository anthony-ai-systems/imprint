"""Provenance gate separating operator-authored transcript entries from synthetic ones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SYNTHETIC_ENTRY_REASON = "synthetic_transcript_entry"
NO_OPERATOR_MESSAGE_REASON = "no_operator_message"
UNVERIFIED_ENTRY_REASON = "unverified_provenance"
MACHINE_ORIGIN_REASON = "machine_origin"

_HUMAN_ORIGIN_KIND = "human"
# Only measured interactive provenance is positive evidence of human input.
# SDK submission establishes transport, not authorship.
_INTERACTIVE_PROMPT_SOURCES = {"typed", "queued", "suggestion_accepted"}
_PROMPT_SOURCE_FIELDS = ("promptSource", "prompt_source")
_SYNTHETIC_FLAGS = ("isSidechain", "isMeta")
_SYNTHETIC_PAYLOADS = ("toolUseResult",)
# Final guard for an entry that carried a positive tell and no structural one:
# skill file bodies, host notifications, injected reminders, and the system
# prompts of imprint's own derivation pipeline.
_MARKERS = (
    "base directory for this skill:",
    "<task-notification>",
    "<system-reminder>",
    "you are a claim distiller",
)
# A marker hiding behind a preamble this short still dominates the entry.
_MARKER_PREAMBLE_CHARS = 256


@dataclass(frozen=True)
class ProvenanceVerdict:
    is_operator: bool
    basis: str


def _origin_kind(entry: Mapping[str, Any]) -> str | None:
    origin = entry.get("origin")
    if isinstance(origin, str):
        return origin.strip().lower() or None
    if isinstance(origin, Mapping):
        kind = origin.get("kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip().lower()
    return None


def _prompt_source(entry: Mapping[str, Any]) -> str | None:
    values = [entry[field].strip().lower() for field in _PROMPT_SOURCE_FIELDS
              if isinstance(entry.get(field), str) and entry[field].strip()]
    if not values:
        return None
    return values[0] if len(set(values)) == 1 else "unverified"


def skip_reason(basis: str | None) -> str:
    if basis == "unverified":
        return UNVERIFIED_ENTRY_REASON
    return SYNTHETIC_ENTRY_REASON if basis else NO_OPERATOR_MESSAGE_REASON


def _is_synthetic_structure(entry: Mapping[str, Any]) -> bool:
    if any(entry.get(field) is not None for field in _SYNTHETIC_PAYLOADS):
        return True
    return any(entry.get(flag) is True for flag in _SYNTHETIC_FLAGS)


def _dominant_marker(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    for marker in _MARKERS:
        index = lowered.find(marker)
        if index < 0:
            continue
        if index == 0 or (
            index <= _MARKER_PREAMBLE_CHARS and (len(stripped) - index) * 2 >= len(stripped)
        ):
            return marker
    return None


def classify_entry_provenance(entry: Mapping[str, Any], text: str) -> ProvenanceVerdict:
    """Trust declared human origin or known interactive transport, never prompt text.

    Explicit origins decide authorship; absent an origin, typed, queued and suggestion_accepted are
    verified human sources. Unknown/SDK prompt sources are unverified. Legacy
    structural/marker diagnostics remain for host entries without a source.
    """
    kind = _origin_kind(entry)
    if kind is not None:
        return ProvenanceVerdict(kind == _HUMAN_ORIGIN_KIND, "origin")
    source = _prompt_source(entry)
    if source is not None:
        if _is_synthetic_structure(entry):
            return ProvenanceVerdict(False, "structure")
        if source in _INTERACTIVE_PROMPT_SOURCES:
            return ProvenanceVerdict(True, "promptSource")
        return ProvenanceVerdict(False, "unverified")
    if _is_synthetic_structure(entry):
        return ProvenanceVerdict(False, "structure")
    if _dominant_marker(text):
        return ProvenanceVerdict(False, "marker")
    return ProvenanceVerdict(False, "structure")
