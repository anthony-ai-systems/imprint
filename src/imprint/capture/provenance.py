"""Provenance gate separating operator-authored transcript entries from synthetic ones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SYNTHETIC_ENTRY_REASON = "synthetic_transcript_entry"

_HUMAN_ORIGIN_KIND = "human"
_SYNTHETIC_FLAGS = ("isSidechain", "isMeta")
_SYNTHETIC_PAYLOADS = ("toolUseResult",)
# Last-resort markers for legacy entries that carry no origin and no structural
# tell: skill file bodies, host notifications, injected reminders, and the
# system prompts of imprint's own derivation pipeline.
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
    """Decide whether one transcript user entry is operator-authored.

    Native entries declare their own provenance, so `origin.kind` decides alone.
    Legacy CLI sessions record no origin at all; those fall back to the
    structural tells of machine-authored turns, then to a marker blacklist that
    is only ever consulted when provenance and structure are both silent.
    """
    kind = _origin_kind(entry)
    if kind is not None:
        return ProvenanceVerdict(kind == _HUMAN_ORIGIN_KIND, "origin")
    if _is_synthetic_structure(entry):
        return ProvenanceVerdict(False, "structure")
    if _dominant_marker(text):
        return ProvenanceVerdict(False, "marker")
    return ProvenanceVerdict(True, "unmarked")
