"""Conservative deterministic explicit-feedback candidate gate."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable


@dataclass(frozen=True)
class FeedbackDetection:
    is_feedback: bool
    route: str
    call_type: str | None
    marker: str | None
    confidence: float


# Rules read a copy with anything not the operator's replaced by filler of its own
# length, so offsets still point into the original. _GAP breaks a sentence the way
# a line end does. _INLINE is an inline quotation, which belongs to its sentence.
_GAP = "\x00"
_INLINE = "\x01"
_BLANKS = " \t\r\n\f\v" + _GAP
# Where a sentence of the operator's can open. A line after a colon is what the
# colon introduced, even across a blank line, but the line after a removed quote
# is the operator's own. Each branch enters a whitespace run at one place only,
# which keeps a long run linear.
_OPENS = r"(?:^|(?<=[.!?\x00])\s*|(?<![:\s])[ \t\r]*\n\s*)"
_RULES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    ("standard", "correct", "forward", re.compile(_OPENS + r"(?:(?:thanks|also|and|great|ok|okay)[,!.]?\s+)?(?:from now on|going forward|from here on)\b(?![^.!?\n]*\?)(?![,:]?\s*(?:should|must)\s+(?:i|we|you|they|it|this|that|the|our|my)\b)[,:]?\s*(?:(?:always|never|only use|use|avoid|stop|don't|do not|make sure|keep|write|put|lead with|include|i want)\b|[^.!?\n]*\b(?:should|must)\b)", re.I)),
    ("refusal", "reject", "rejection", re.compile(r"\b(?:i reject|reject (?:this|that|it))\b", re.I)),
    ("refusal", "refuse", "refusal", re.compile(r"\b(?:do not|don't|won't)\s+(?:use|include|change|send|publish|do|make)|\b(?:i refuse|no thanks|stop (?:using|doing))\b", re.I)),
    ("preference", "prefer", "preference", re.compile(r"\b(?:i prefer|i'd rather|i would rather|my preference is|favor [^\n]{1,200}? over)\b", re.I)),
    ("approval", "accept", "approval", re.compile(r"\b(?:approved|exactly right|that's right|that is right|looks good|perfect|ship it|this is the one)\b", re.I)),
    ("standard", "correct", "standard", re.compile(r"\b(?:(?:always|never)\s+(?:use|include|remove|change|keep|do|say|write)|(?:we|you|it|this) must|(?<!\bif )(?<!\bwhether )(?<!\bwhen )(?:(?:it|this|that) needs to|(?:these|those|they|you) need to|(?:the|your|each|every) [\w-]+ needs? to)\b(?![^.!?\n]{0,200}\?)|the standard is|our rule is)\b", re.I)),
    ("correction", "correct", "direct", re.compile(_OPENS + r"(?:no[,.:]|wrong\b|incorrect\b)|\b(?:(?:this|that|it) is wrong|that's wrong|not [^\n]{1,200}?[,;:]? (?:use|make|keep)|instead\b|change (?:it|that|this)|you (?:missed|changed|removed|added))\b", re.I)),
    ("correction", "correct", "question_form", re.compile(r"\b(?:why did you|shouldn't (?:it|this|that)|wouldn't (?:it|this|that) be|can you (?:change|restore|remove|keep)|could you (?:change|restore|remove|keep))\b", re.I)),
    ("correction", "correct", "indirect", re.compile(r"\b(?:not quite|this feels (?:too|like)|it would be better|what i (?:meant|was looking for)|this isn't landing|this is not landing)\b", re.I)),
)
_POLITE_EDIT = re.compile(r"\b(?:please|could you|would you)\b[^\n]{0,400}\b(?:change|use|make|keep|remove|restore|avoid|don't)\b", re.I)
# These read as corrections only as a reply to something the assistant produced,
# so unlike _RULES they require prior assistant output. "It should be" alone is
# as often a closing remark as a target, so only forms that name a target count,
# less the words after them that state an expectation.
_REPLY_CORRECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("short_should", re.compile(r"\A\s*(?:no[,.]?\s+)?(?:it|this|that|these|those|they)\s+(?:should|ought to)\s+(?:be\s+(?:a|an|the|plain|in)\b|have been|say|read|use)\b(?!\s+(?:a\s+|an\s+|the\s+)?(?:quick|easy|fine|ok|okay|good|done|ready|fast|simple|straightforward|safe|working|able|possible|there|finished|live|up|last|end|case|place|while|few|couple)\b)[^.?!\n]{0,60}[.!]?\s*\Z", re.I)),
    ("why_should", re.compile(_OPENS + r"why\b[^.?!\n]{0,120}\b(?:should (?:be|have been)|should've been)\b(?![^.!?\n]*\?)", re.I)),
)
# Too wide to capture a message, but enough to keep a sentence of one already
# captured: a correction often opens with a contrast or an edit, or names what is
# wrong in other words than the rules do.
_LESSON_SIGNS = re.compile(
    _OPENS + r"(?:(?:but|however|except|that said)\b|(?:please\s+)?(?:remove|delete|drop|replace|rename|fix|revert|restore)\b)"
    r"|\b(?:is|are|was|were|looks?|seems?)\s+(?:wrong|incorrect|broken)\b",
    re.I,
)
# The host prepends its own blocks to a genuinely submitted prompt, which pushes
# the operator's first sentence off the start of the text and out of reach of
# the sentence-anchored correction rule. Only *leading* blocks are removed: a
# marker later in the text is the operator quoting or discussing one.
_LEADING_HOST_BLOCK = re.compile(
    r"\A\s*(?:<(system-reminder|task-notification)\b[^>]*>.*?</\1>\s*)+",
    re.I | re.S,
)
# T3 adds this for Ultrathink effort, in the composer or only at send time. It is
# not the operator's word, and the first sentence comes after it.
_EFFORT_PREFIX = re.compile(r"\A[\s\x00]*Ultrathink:\n")


def _host_block(tag: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    closer = rf"</{tag}(?:\s[^>]{{0,1024}})?>"
    return re.compile(closer, re.I), re.compile(rf"<{tag}\b[^>]{{0,1024}}>.*?{closer}", re.I | re.S)


# T3 carries pasted text and attachment details inside the operator's message.
# None of it is the operator speaking, wherever it sits. Claude Code closes a
# paste with its id, as in </pasted_content id="ab12">. One pattern per tag runs
# only as far as that tag's last closer, so an unclosed tag cannot make every
# opener scan the rest of the text.
_EMBEDDED_HOST_BLOCKS = (_host_block("pasted_content"), _host_block("t3_context"))
# One bracketed span and nothing else: a line that starts with one and goes on
# is the operator talking about the attachment.
_ATTACHMENT_LINE = re.compile(r"^[ \t]*\[(?:File|Image|Attached (?:file|image))\b[^\]\n]*\][ \t]*$", re.M)


def _delimited_by(run: str) -> str:
    """Match between two runs, or from a lone run a colon introduces.

    Any other lone run is a heading underline or a divider, and hides nothing.
    The second branch cannot fail once it has a run, so neither branch rescans.
    """
    return rf"{run}.*?{run}|(?<=:)\s*{run}.*?(?:{run}|\Z)"


# Quoted spans are someone else's words, even inside the operator's message.
# Delimited pastes stand apart from the operator's sentences, so they also leave
# the stored text; an inline quotation stays because it is part of its sentence.
# A code fence and a --- block stay too: both are as often the operator's own.
_DELIMITED_PASTE = r"^[ \t]*>[^\n]*$|" + _delimited_by("(?<!=)={4,}(?!=)")
_DELIMITED_SPAN = re.compile(_DELIMITED_PASTE, re.M | re.S)
_INLINE_QUOTE = (
    r'"[^"\n]{3,}(?:\n[^"\n]+)*\n?"'
    r"|\u201c[^\u201c\u201d\n]{3,}(?:\n[^\u201c\u201d\n]+)*\n?\u201d"
    r"|(?<![\w'])'[^'\n]{3,}'(?![\w'])"
)
# An unclosed fence runs to the end of the text, so an opener never rescans.
_CODE_FENCE = r"^[ \t]*(?P<fence>`{3,}|~{3,})[^`\n]*$.*?(?:^[ \t]*(?P=fence)[`~]*[ \t]*$|\Z)"
_DASH_LINE = r"^[ \t]*-{3,}[ \t]*$"
_QUOTED_SPAN = re.compile(
    f"(?P<inline>{_INLINE_QUOTE})|{_CODE_FENCE}|{_delimited_by(_DASH_LINE)}|{_DELIMITED_PASTE}",
    re.M | re.S,
)
# A full stop inside a file name or a version number ends nothing.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])(?=[\s\x00]|\Z)|\n+|\x00+")


_MIN_SENTENCE_CHARS = 20


def strip_leading_host_blocks(text: str) -> str:
    """Return the operator's own words with prepended host blocks removed."""
    return _LEADING_HOST_BLOCK.sub("", text)


def strip_effort_prefix(text: str) -> str:
    """Return the text without one leading effort prefix."""
    return _EFFORT_PREFIX.sub("", text.lstrip())


def _sub_embedded_blocks(replacement: str | Callable[[re.Match[str]], str], text: str) -> str:
    for closer, block in _EMBEDDED_HOST_BLOCKS:
        end = max((found.end() for found in closer.finditer(text)), default=0)
        text = block.sub(replacement, text[:end]) + text[end:]
    return text


def strip_embedded_host_content(text: str) -> str:
    """Return the message without T3's pasted blocks and attachment lines."""
    return _ATTACHMENT_LINE.sub("", _sub_embedded_blocks("", text)).strip()


def operator_words(text: str) -> str:
    """Return a prompt without what the host put around the operator's words."""
    return strip_embedded_host_content(strip_effort_prefix(strip_leading_host_blocks(text)))


def strip_delimited_pastes(text: str) -> str:
    """Return the message without block quotes and spans between runs of equals signs."""
    return _DELIMITED_SPAN.sub("", text).strip()


def _blanked(span: re.Match[str]) -> str:
    return "\n".join(_GAP * len(line) for line in span.group().split("\n"))


def _blanked_quote(span: re.Match[str]) -> str:
    return _INLINE * len(span.group()) if span.group("inline") else _blanked(span)


def _masked(text: str) -> str:
    """Return ``text`` at its own length with only the operator's words readable."""
    masked = _EFFORT_PREFIX.sub(_blanked, _LEADING_HOST_BLOCK.sub(_blanked, text))
    masked = _ATTACHMENT_LINE.sub(_blanked, _sub_embedded_blocks(_blanked, masked))
    return _QUOTED_SPAN.sub(_blanked_quote, masked)


def _sentence_spans(spoken: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for stop in _SENTENCE_BREAK.finditer(spoken):
        spans.append((start, stop.start()))
        start = stop.end()
    spans.append((start, len(spoken)))
    return spans


def feedback_sentences(text: str) -> list[str]:
    """Return every sentence of the operator's that reads as feedback, in order.

    Stored judgments are whole messages, and a later sentence can overrule an
    earlier one, so no rule outranks another here. Rules read a masked copy of
    the same length and each result is sliced from ``text``, so it is always an
    exact part of it. A match that runs past its sentence is passed over. Short
    sentences count only beside one that can stand alone: "That is wrong." says
    a lot after what it rejects and nothing as the whole of a summary.

    This chooses from a record already captured, so it is wider than detection.
    """
    masked = _masked(text).replace(_INLINE, " ")
    spoken = masked.lstrip(_BLANKS)
    shift = len(masked) - len(spoken)
    spans = _sentence_spans(spoken)
    starts = [start for start, _ in spans]
    matched = set()
    for pattern in (*(rule[3] for rule in _RULES), _LESSON_SIGNS):
        for match in pattern.finditer(spoken):
            first_word = match.end() - len(match.group().lstrip())
            span = spans[bisect_right(starts, first_word) - 1]
            if match.end() <= span[1]:
                matched.add(span)
    sentences = [text[shift + start: shift + end].strip() for start, end in sorted(matched)]
    return sentences if any(len(sentence) >= _MIN_SENTENCE_CHARS for sentence in sentences) else []


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
    spoken = _masked(operator_text).replace(_INLINE, "").strip(_BLANKS)
    if not spoken:
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
