"""The detector runs inside hooks killed at 10 seconds, on text of any shape."""
import time

import pytest

from imprint.capture.detector import (
    detect_explicit_feedback,
    feedback_sentences,
    operator_words,
    strip_delimited_pastes,
)

SIZE = 170_000
# Far above the worst case measured for these inputs, about 0.15 seconds, and far
# below what a quadratic path costs at this size. Process time ignores other load.
BOUND_SECONDS = 2.5


def _repeated(unit, end=""):
    return (unit * (SIZE // len(unit) + 1))[:SIZE] + end


ADVERSARIAL = {
    "newline run": "x" + "\n" * SIZE + "y",
    "newline run after feedback": "No, change it to the short label.\n" + "\n" * SIZE + "That is all.",
    "crlf run": "x" + "\r\n" * (SIZE // 2) + "y",
    "space and newline run": "x" + " \n" * (SIZE // 2) + "y",
    "space run": "End." + " " * SIZE,
    "paragraphs and blank runs": ("A plain paragraph of words.\n" * 40 + "\n" * 4000) * 20,
    "block quote lines": _repeated("> ok\n"),
    "not, then a semicolon": "Looks good.\n" + _repeated("if not x: y = z; "),
    "not in a log line": _repeated("ERROR 2026-10-30 file not found at path; "),
    "not": _repeated("not "),
    "favor": _repeated("favor "),
    "favor and spaces": _repeated("favor    "),
    "please": _repeated("please "),
    "could you": _repeated("could you "),
    "curly word": _repeated("\u201cword "),
    "curly letter": _repeated("\u201ca"),
    "curly opener": _repeated("\u201c"),
    "t3 opener": _repeated("<t3_context>"),
    "paste opener": _repeated("<pasted_content>"),
    "closer before openers": "</t3_context>" + _repeated("<t3_context>"),
    "opener with no bracket": _repeated("<t3_context "),
    "opener with no bracket before a closer": _repeated("<t3_context ", "</t3_context>"),
    "closer with no bracket": "<pasted_content>" + _repeated("</pasted_content "),
    "other tag closes": _repeated("<pasted_content></t3_context>"),
    "unclosed host block": _repeated("<system-reminder>"),
    "single quote": _repeated("'a "),
    "single quote word": _repeated("'abcd "),
    "double quote": _repeated('"'),
    "double quote and lines": '"' + _repeated("a\n"),
    "double quote paragraphs": _repeated('"abc\n\n'),
    "fence": _repeated("```\n"),
    "fence with info": _repeated("```a\n"),
    "tilde fence": _repeated("~~~\n"),
    "backtick run": "`" * SIZE + "x`",
    "dash line": _repeated("---\n"),
    "dash line after colon": _repeated("a:\n---\n"),
    "equals runs": _repeated("==== "),
    "short equals runs": _repeated("======= x "),
    "equals after colon": _repeated("a: ==== "),
    "colons": _repeated(": "),
    "colon lines": _repeated(":\n"),
    "stops": _repeated(". "),
    "stop lines": _repeated(".\n"),
    "needs to before a question mark": _repeated("it needs to ", "?"),
    "subject needs to before a question mark": _repeated("the x needs to ", "?"),
    "forward lead before a question mark": _repeated("from now on ", "?"),
    "forward lead lines": _repeated("from now on x\n", "?"),
    "should before a question mark": "Going forward " + _repeated("should ", "?"),
    "why before a question mark": "Why " + _repeated("should be ", "?"),
    "why lines": _repeated("why should be x\n"),
    "short sentences that match": _repeated("instead. "),
    "matches past their sentence": _repeated("not a. b use "),
    "attachment lines": _repeated("[File x]\n"),
    "unclosed attachment": _repeated("[File "),
}


@pytest.mark.parametrize("name", ADVERSARIAL)
@pytest.mark.parametrize("step", [
    lambda text: detect_explicit_feedback(text, prior_assistant_output="Draft."),
    feedback_sentences,
    lambda text: strip_delimited_pastes(operator_words(text)),
], ids=["detect", "sentence", "strip"])
def test_adversarial_text_stays_far_inside_the_hook_limit(step, name):
    started = time.process_time()
    step(ADVERSARIAL[name])
    assert time.process_time() - started < BOUND_SECONDS


def test_a_correction_before_a_blank_run_is_still_detected():
    assert detect_explicit_feedback(ADVERSARIAL["newline run after feedback"]).marker == "direct"
