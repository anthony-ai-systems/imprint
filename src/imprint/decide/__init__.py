"""Typed decisions: Jev's Noul, Choice and Score questions for named purposes.

Nothing here is switched on. A purpose sends only when
``org.imprint.decide.purposes`` names it with a mode of ``shadow`` or ``enforce``
and a daily cap above zero, and the shipped config names none. The live selector
(``imprint.retrieve.jev``) does not use this package, and no hook imports it; a
test holds both.
"""

from __future__ import annotations

from .client import (
    COUNTER_NAME,
    MAX_QUESTIONS,
    MAX_REQUEST_BYTES,
    OUTCOMES,
    REASONS,
    STATE_LIMIT,
    DecisionResult,
    ask,
    calls_today,
    counter_path,
    decision_entry,
    decision_log_path,
    encode_request,
    request_body,
)
from .purposes import DECIDE_ENV, MODES, PURPOSES, enabled_purposes, purpose_mode, switched_off
from .questions import (
    Answer,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
    choice,
    noul,
    score,
)

__all__ = [
    "Answer",
    "COUNTER_NAME",
    "Choice",
    "ChoiceAnswer",
    "DECIDE_ENV",
    "DecisionResult",
    "MAX_QUESTIONS",
    "MAX_REQUEST_BYTES",
    "MODES",
    "Noul",
    "NoulAnswer",
    "OUTCOMES",
    "PURPOSES",
    "Question",
    "REASONS",
    "STATE_LIMIT",
    "Score",
    "ScoreAnswer",
    "ask",
    "calls_today",
    "choice",
    "counter_path",
    "decision_entry",
    "decision_log_path",
    "enabled_purposes",
    "encode_request",
    "noul",
    "purpose_mode",
    "request_body",
    "score",
    "switched_off",
]
