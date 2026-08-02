import pytest

from imprint.capture.detector import detect_explicit_feedback


@pytest.mark.parametrize("text, marker, route", [
    ("No, use the compact synthetic card.", "direct", "correction"),
    ("Why did you remove the neutral heading?", "question_form", "correction"),
    ("This is not landing; it feels too broad.", "indirect", "correction"),
    ("I prefer the neutral version over the ornate one.", "preference", "preference"),
    ("We must keep source references on every claim.", "standard", "standard"),
    ("Approved. Ship it.", "approval", "approval"),
    ("I reject this synthetic draft.", "rejection", "refusal"),
    ("Do not publish that synthetic example.", "refusal", "refusal"),
])
def test_explicit_feedback_forms(text, marker, route):
    result = detect_explicit_feedback(text, prior_assistant_output="synthetic output")
    assert result.is_feedback and result.marker == marker and result.route == route


def test_polite_feedback_requires_prior_output():
    text = "Please keep the second heading and remove the first."
    assert detect_explicit_feedback(text).is_feedback is False
    assert detect_explicit_feedback(text, prior_assistant_output="a draft").marker == "polite"


def test_silent_reask_is_an_operator_reask_not_silence():
    result = detect_explicit_feedback(
        "Create a concise neutral summary with source labels.",
        prior_operator_text="Create a concise neutral summary with source labels",
        prior_assistant_output="an unrelated output",
    )
    assert result.is_feedback and result.marker == "silent_reask"
    assert detect_explicit_feedback("", prior_assistant_output="anything").is_feedback is False


@pytest.mark.parametrize("text", [
    "What time is the synthetic review?", "I don't know the answer.",
    "I have never visited that place.", "Could you create a new summary?",
    "Thanks for the update.", "No idea where the fixture lives.",
])
def test_negative_controls(text):
    assert detect_explicit_feedback(text).is_feedback is False


REMINDER = (
    "<system-reminder>The task tools have not been used recently. This is a "
    "gentle reminder; ignore it if it does not apply.</system-reminder>"
)


def test_a_prepended_host_block_does_not_hide_the_operator_sentence():
    # The host prepends its blocks to a genuinely submitted prompt. Without
    # stripping them the correction rule's sentence anchor never reaches the
    # operator's opening "No,".
    result = detect_explicit_feedback(f"{REMINDER}\n\nNo, keep the failed source in the summary.")
    assert result.is_feedback and result.marker == "direct"
    assert detect_explicit_feedback(
        f"<task-notification>scout finished</task-notification>\n{REMINDER}\n\n"
        "No, keep the failed source in the summary."
    ).is_feedback


def test_a_host_block_only_turn_is_not_operator_feedback():
    assert detect_explicit_feedback(REMINDER).is_feedback is False


def test_only_leading_host_blocks_are_stripped():
    # A marker the operator quotes mid-sentence stays part of what was said.
    quoted = f"The reminder reads {REMINDER} and it is wrong."
    assert detect_explicit_feedback(quoted).is_feedback
    trailing = f"No, keep the failed source in the summary.\n\n{REMINDER}"
    assert detect_explicit_feedback(trailing).marker == "direct"
