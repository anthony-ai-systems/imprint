"""Typed questions for Jev's three documented primitives, and their answers.

A question carries only what code put in it. Names (of questions and of Choice
options) are identifiers chosen in code, never text from a record, a prompt or a
person, so they can be logged.

What is confirmed and what is not, against the live API:

- Noul. Request ``{"type": "noul", "instructions": ...}`` and answer
  ``{"type": "noul", "noul": p}``: confirmed by the live selector
  (``imprint.retrieve.jev``), which has sent and parsed exactly this shape.
- Choice. Request ``{"type": "choice", "instructions": ..., "criteria": {name:
  description}}`` (1 to 255 options) and answer ``{"type": "choice", "choice":
  name, "confidence": c, "probabilities": ...}``: from TypeSafe's public OpenAPI
  document and docs only. Unverified against the live API.
- Score. Request ``{"type": "score", "instructions": ..., "criteria": [level, ...]}``
  (1 to 10 levels, numbered from 0) and answer ``{"type": "score", "score": s,
  "confidence": c, "legend": ..., "probabilities": ...}``, where ``score`` is the
  expected level and can fall between levels: from the public docs only.
  Unverified against the live API.

The public docs do not pin the shape of ``probabilities`` for Choice or Score, so
both an object keyed by option name or level number and an array in request order
are read. Parsing is defensive: a field the answer does not need is ignored, a
required field that is missing or out of range rejects the answer, an optional
field that is present must be valid or the answer is rejected too, and a Choice
naming an option the request did not send is rejected.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Union

INSTRUCTIONS_LIMIT = 4000
DESCRIPTION_LIMIT = 1000
MAX_OPTIONS = 255
MAX_LEVELS = 10
# Question and option names: identifiers only, so a log can carry them.
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{what} must be non-empty text")
    if len(value) > limit:
        raise ValueError(f"{what} must be at most {limit} characters")
    return value


def probability(value: Any) -> float | None:
    """A finite number in 0..1, or None. JSON booleans are never numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def _optional_probability(answer: Mapping[str, Any], field: str) -> tuple[bool, float | None]:
    """(valid, value) for an optional probability field."""
    if field not in answer or answer[field] is None:
        return True, None
    value = probability(answer[field])
    return value is not None, value


@dataclass(frozen=True)
class NoulAnswer:
    p: float
    type: str = "noul"

    def values(self) -> dict[str, object]:
        return {"noul": round(self.p, 4)}


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float | None = None
    probabilities: Mapping[str, float] | None = None
    type: str = "choice"

    def values(self) -> dict[str, object]:
        value: dict[str, object] = {"choice": self.choice, "confidence": _rounded(self.confidence)}
        if self.probabilities is not None:
            value["probabilities"] = {name: round(p, 4) for name, p in self.probabilities.items()}
        return value


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    confidence: float | None = None
    probabilities: tuple[float, ...] | None = None
    type: str = "score"

    def values(self) -> dict[str, object]:
        value: dict[str, object] = {"score": round(self.score, 4), "confidence": _rounded(self.confidence)}
        if self.probabilities is not None:
            value["probabilities"] = [round(p, 4) for p in self.probabilities]
        return value


Answer = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer]


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


@dataclass(frozen=True)
class Noul:
    """A yes/no question. Its answer is the probability of yes."""

    instructions: str
    type = "noul"

    def __post_init__(self) -> None:
        _text(self.instructions, "noul instructions", INSTRUCTIONS_LIMIT)

    def request(self) -> dict[str, object]:
        return {"type": "noul", "instructions": self.instructions}

    def parse(self, answer: Any) -> NoulAnswer | None:
        if not isinstance(answer, Mapping) or answer.get("type") != "noul":
            return None
        value = probability(answer.get("noul"))
        return None if value is None else NoulAnswer(value)


@dataclass(frozen=True)
class Choice:
    """One of named options, each with a description. Order is kept as given:
    the public docs say Jev leans toward the first option."""

    instructions: str
    options: tuple[tuple[str, str], ...]
    type = "choice"

    def __post_init__(self) -> None:
        _text(self.instructions, "choice instructions", INSTRUCTIONS_LIMIT)
        if not isinstance(self.options, tuple) or not 1 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"a choice needs 1..{MAX_OPTIONS} options")
        names = [name for name, _ in self.options]
        if len(set(names)) != len(names):
            raise ValueError("choice option names must be unique")
        for name, description in self.options:
            if not isinstance(name, str) or NAME.fullmatch(name) is None:
                raise ValueError("a choice option name must be a lowercase identifier")
            _text(description, f"choice option {name} description", DESCRIPTION_LIMIT)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.options)

    def request(self) -> dict[str, object]:
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.options)}

    def parse(self, answer: Any) -> ChoiceAnswer | None:
        if not isinstance(answer, Mapping) or answer.get("type") != "choice":
            return None
        names = self.names
        chosen = answer.get("choice")
        if not isinstance(chosen, str) or chosen not in names:
            return None  # an option the request did not send
        valid, confidence = _optional_probability(answer, "confidence")
        if not valid:
            return None
        raw = answer.get("probabilities")
        probabilities: dict[str, float] | None = None
        if isinstance(raw, Mapping):
            if any(not isinstance(name, str) or name not in names for name in raw):
                return None
            probabilities = {}
            for name, value in raw.items():
                p = probability(value)
                if p is None:
                    return None
                probabilities[name] = p
        elif isinstance(raw, list):
            if len(raw) != len(names):
                return None
            values = [probability(value) for value in raw]
            if any(value is None for value in values):
                return None
            probabilities = dict(zip(names, values))  # type: ignore[arg-type]
        elif raw is not None:
            return None
        return ChoiceAnswer(chosen, confidence, probabilities)


@dataclass(frozen=True)
class Score:
    """A place on an ordered rubric. Level 0 is the first description."""

    instructions: str
    levels: tuple[str, ...]
    type = "score"

    def __post_init__(self) -> None:
        _text(self.instructions, "score instructions", INSTRUCTIONS_LIMIT)
        if not isinstance(self.levels, tuple) or not 1 <= len(self.levels) <= MAX_LEVELS:
            raise ValueError(f"a score needs 1..{MAX_LEVELS} levels")
        for index, level in enumerate(self.levels):
            _text(level, f"score level {index}", DESCRIPTION_LIMIT)

    def request(self) -> dict[str, object]:
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.levels)}

    def parse(self, answer: Any) -> ScoreAnswer | None:
        if not isinstance(answer, Mapping) or answer.get("type") != "score":
            return None
        top = len(self.levels) - 1
        value = answer.get("score")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(value) or not 0.0 <= value <= top:
            return None
        valid, confidence = _optional_probability(answer, "confidence")
        if not valid:
            return None
        raw = answer.get("probabilities")
        probabilities: tuple[float, ...] | None = None
        if isinstance(raw, list):
            if len(raw) != len(self.levels):
                return None
            values = [probability(item) for item in raw]
            if any(item is None for item in values):
                return None
            probabilities = tuple(values)  # type: ignore[arg-type]
        elif isinstance(raw, Mapping):
            levels = [0.0] * len(self.levels)
            for key, item in raw.items():
                if not isinstance(key, str) or not key.isdigit() or int(key) > top:
                    return None
                p = probability(item)
                if p is None:
                    return None
                levels[int(key)] = p
            probabilities = tuple(levels)
        elif raw is not None:
            return None
        return ScoreAnswer(value, confidence, probabilities)


Question = Union[Noul, Choice, Score]
QUESTION_TYPES = (Noul, Choice, Score)


def noul(instructions: str) -> Noul:
    """A Noul: does the statement in ``instructions`` hold for the state?"""
    return Noul(instructions)


def choice(instructions: str, options: Mapping[str, str]) -> Choice:
    """A Choice over ``options``, name to description, in the order given."""
    if not isinstance(options, Mapping):
        raise ValueError("choice options must map names to descriptions")
    return Choice(instructions, tuple(options.items()))


def score(instructions: str, levels: Sequence[str]) -> Score:
    """A Score on ``levels``, lowest first."""
    if isinstance(levels, (str, bytes)) or not isinstance(levels, Sequence):
        raise ValueError("score levels must be a sequence of descriptions")
    return Score(instructions, tuple(levels))


__all__ = [
    "Answer",
    "Choice",
    "ChoiceAnswer",
    "NAME",
    "Noul",
    "NoulAnswer",
    "QUESTION_TYPES",
    "Question",
    "Score",
    "ScoreAnswer",
    "choice",
    "noul",
    "probability",
    "score",
]
