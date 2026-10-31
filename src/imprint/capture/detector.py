"""Conservative deterministic explicit-feedback candidate gate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class FeedbackDetection:
    is_feedback: bool
    route: str
    call_type: str | None
    marker: str | None
    confidence: float


_RULES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    ("standard", "correct", "forward", re.compile(r"(?:^|[.!?\n]\s*)(?:from now on|going forward|from here on)\b[^.!?\n]*\b(?:should|must|always|never|only|use|avoid|stop|don't|do not|make sure)\b", re.I)),
    ("refusal", "reject", "rejection", re.compile(r"\b(?:i reject|reject (?:this|that|it))\b", re.I)),
    ("refusal", "refuse", "refusal", re.compile(r"\b(?:do not|don't|won't)\s+(?:use|include|change|send|publish|do|make)|\b(?:i refuse|no thanks|stop (?:using|doing))\b", re.I)),
    ("preference", "prefer", "preference", re.compile(r"\b(?:i prefer|i'd rather|i would rather|my preference is|favor .+ over)\b", re.I)),
    ("approval", "accept", "approval", re.compile(r"\b(?:approved|exactly right|that's right|that is right|looks good|perfect|ship it|this is the one)\b", re.I)),
    ("standard", "correct", "standard", re.compile(r"\b(?:(?:always|never)\s+(?:use|include|remove|change|keep|do|say|write)|(?:we|you|it|this) must|(?:it|this|that|these|those) needs? to|the standard is|our rule is)\b", re.I)),
    ("correction", "correct", "direct", re.compile(r"(?:^|[.!?]\s*)(?:no[,.:]|wrong\b|incorrect\b)|\b(?:(?:this|that|it) is wrong|that's wrong|not .+[,;:]? (?:use|make|keep)|instead\b|change (?:it|that|this)|you (?:missed|changed|removed|added))\b", re.I)),
    ("correction", "correct", "question_form", re.compile(r"\b(?:why did you|shouldn't (?:it|this|that)|wouldn't (?:it|this|that) be|can you (?:change|restore|remove|keep)|could you (?:change|restore|remove|keep))\b", re.I)),
    ("correction", "correct", "indirect", re.compile(r"\b(?:not quite|this feels (?:too|like)|it would be better|what i (?:meant|was looking for)|this isn't landing|this is not landing)\b", re.I)),
)
_POLITE_EDIT = re.compile(r"\b(?:please|could you|would you)\b.*\b(?:change|use|make|keep|remove|restore|avoid|don't)\b", re.I)
# These read as corrections only as a reply to something the assistant produced,
# so unlike _RULES they require prior assistant output. Words after "should be"
# that state an expectation rather than a target are excluded.
_REPLY_CORRECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("short_should", re.compile(r"\A\s*(?:no[,.]?\s+)?(?:it|this|that|these|those|they)\s+(?:should|ought to)\s+(?:be|have been|say|read|use|go)\b(?!\s+(?:a\s+|an\s+|the\s+)?(?:quick|easy|fine|ok|okay|good|done|ready|fast|simple|straightforward|safe|working|able|possible|there|finished|live|up)\b)[^.?!\n]{0,60}[.!]?\s*\Z", re.I)),
    ("why_should", re.compile(r"(?:^|[.!?\n]\s*)why\b[^.?!\n]{0,120}\b(?:should (?:be|have been)|should've been)\b", re.I)),
)
# The host prepends its own blocks to a genuinely submitted prompt, which pushes
# the operator's first sentence off the start of the text and out of reach of
# the sentence-anchored correction rule. Only *leading* blocks are removed: a
# marker later in the text is the operator quoting or discussing one.
_LEADING_HOST_BLOCK = re.compile(
    r"\A\s*(?:<(system-reminder|task-notification)\b[^>]*>.*?</\1>\s*)+",
    re.I | re.S,
)
# T3 carries pasted text and attachment details inside the operator's message.
# None of it is the operator speaking, wherever it sits.
_EMBEDDED_HOST_BLOCK = re.compile(r"<(t3_context|pasted_content)\b[^>]*>.*?</\1>", re.I | re.S)
_ATTACHMENT_LINE = re.compile(r"^[ \t]*\[(?:File|Image|Attached file)\b[^\n]*\][ \t]*$", re.M)
# Quoted spans are someone else's words, even inside the operator's message.
# So is text pasted after a run of equals signs, up to the next run or the end.
_QUOTED_SPAN = re.compile(
    r'"[^"\n]{3,}"|\u201c[^\u201d\n]{3,}\u201d|^[ \t]*>[^\n]*$|={4,}.*?(?:={4,}|\Z)',
    re.M | re.S,
)


def strip_leading_host_blocks(text: str) -> str:
    """Return the operator's own words with prepended host blocks removed."""
    return _LEADING_HOST_BLOCK.sub("", text)


def strip_embedded_host_content(text: str) -> str:
    """Return the message without T3's pasted blocks and attachment lines."""
    return _ATTACHMENT_LINE.sub("", _EMBEDDED_HOST_BLOCK.sub("", text)).strip()


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def detect_explicit_feedback(
    operator_text: str,
    *,
    prior_operator_text: str | None = None,
    prior_assistant_output: str | None = None,
) -> FeedbackDetection:
    """Detect feedback forms without treating silence or ordinary questions as calls."""
    if not isinstance(operator_text, str) or not operator_text.strip():
        return FeedbackDetection(False, "non_feedback", None, None, 1.0)
    spoken = _QUOTED_SPAN.sub("", strip_embedded_host_content(strip_leading_host_blocks(operator_text)))
    if not spoken.strip():
        return FeedbackDetection(False, "non_feedback", None, None, 1.0)
    for route, call_type, marker, pattern in _RULES:
        if pattern.search(spoken):
            return FeedbackDetection(True, route, call_type, marker, 0.99)
    if prior_assistant_output:
        for marker, pattern in _REPLY_CORRECTIONS:
            if pattern.search(spoken):
                return FeedbackDetection(True, "correction", "correct", marker, 0.96)
    if prior_assistant_output and _POLITE_EDIT.search(spoken):
        return FeedbackDetection(True, "correction", "correct", "polite", 0.96)
    if prior_assistant_output and prior_operator_text:
        current, prior = _normalize(spoken), _normalize(prior_operator_text)
        if current and prior:
            ratio = SequenceMatcher(None, prior, current).ratio()
            shared = len(set(current.split()) & set(prior.split())) / max(1, len(set(prior.split())))
            if ratio >= 0.88 or (ratio >= 0.72 and shared >= 0.8):
                return FeedbackDetection(True, "correction", "correct", "silent_reask", round(max(ratio, shared), 3))
    return FeedbackDetection(False, "non_feedback", None, None, 0.99)
