"""Provenance gate separating operator-authored transcript entries from synthetic ones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SYNTHETIC_ENTRY_REASON = "synthetic_transcript_entry"
NO_OPERATOR_MESSAGE_REASON = "no_operator_message"

_HUMAN_ORIGIN_KIND = "human"
# The host stamps every entry it created from a submitted prompt with
# promptSource (measured: 10/10 prompt entries carry it, 0/54 tool-result
# entries do). It is the positive tell that an entry is a prompt at all.
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


def _has_prompt_source(entry: Mapping[str, Any]) -> bool:
    for field in _PROMPT_SOURCE_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return True
    return False


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

    The gate is ordered positive tell first. Native entries declare themselves,
    so ``origin.kind`` decides alone. Otherwise ``promptSource`` decides that the
    entry is a submitted prompt, and markers are deliberately *not* consulted for
    it: the host injects reminders into real prompt entries, so a marker there
    would drop operator speech. An entry carrying neither tell is not a prompt
    entry at all -- it is a tool result, an interruption notice, or another
    host-authored turn -- and is synthetic regardless of what it says. The marker
    blacklist survives only as a final guard, and as the reported basis when one
    of those host-authored shapes is also recognisable by its text.
    """
    kind = _origin_kind(entry)
    if kind is not None:
        return ProvenanceVerdict(kind == _HUMAN_ORIGIN_KIND, "origin")
    if _has_prompt_source(entry):
        if _is_synthetic_structure(entry):
            return ProvenanceVerdict(False, "structure")
        return ProvenanceVerdict(True, "promptSource")
    if _is_synthetic_structure(entry):
        return ProvenanceVerdict(False, "structure")
    if _dominant_marker(text):
        return ProvenanceVerdict(False, "marker")
    return ProvenanceVerdict(False, "structure")
