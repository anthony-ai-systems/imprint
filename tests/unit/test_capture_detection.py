import pytest

from imprint.capture.detector import (
    detect_explicit_feedback,
    feedback_sentences,
    operator_words,
    strip_delimited_pastes,
    strip_embedded_host_content,
)


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


def _fires(text):
    return detect_explicit_feedback(text, prior_assistant_output="Draft.").is_feedback


@pytest.mark.parametrize("text", [
    "That should be enough.",
    "That should be all.",
    "That should be it.",
    "That should be everything.",
    "It should be fixed now.",
    "This should be correct.",
    "That should be right.",
    "They should be back on Monday.",
    "It should be here tomorrow.",
    "It should go through now.",
    "That should be the last of it.",
    "It should be in place by Friday.",
    "It should be a few minutes.",
])
def test_closing_remarks_are_not_corrections(text):
    assert _fires(text) is False


@pytest.mark.parametrize("text", [
    "It should be a table.",
    "It should be plain text.",
    "They should be in date order.",
    "It should say the source name.",
    "That should read as a list.",
    "It should use the smaller card.",
    "This should have been a table.",
])
def test_a_reply_naming_a_target_is_a_correction(text):
    assert detect_explicit_feedback(text, prior_assistant_output="Draft.").marker == "short_should"


@pytest.mark.parametrize("text", [
    "Going forward, should we use the new template?",
    "From now on should we use tabs?",
    "From now on should we use tabs",
    "From now on, should we use tabs",
    "Going forward: should I keep the header",
    "From here on must the summary stay short",
    "Going forward, what should the default be?",
    "From here on, do we only need the summary?",
    "Going forward the API will only return JSON.",
    "From now on the job runs only on weekdays.",
    "Going forward, the team plans to use Postgres.",
    "Going forward, lead times will be longer.",
    "Going forward, only the summary changes.",
    "Add this to the doc:\nGoing forward the service must retry.",
    "Add this to the doc:\n\nGoing forward the service must retry.",
    "Add this to the doc:\r\n\r\nGoing forward the service must retry.",
    "Add this to the doc:  \n  \n  Going forward the service must retry.",
])
def test_forward_questions_descriptions_and_reported_lines_are_not_standards(text):
    assert _fires(text) is False


@pytest.mark.parametrize("text", [
    "Thanks, from now on use short headings.",
    "Also, from now on use short headings.",
    "And going forward use plain text.",
    "Great, from here on use the smaller card.",
    "From now on keep it to one page.",
    "From now on, write in the past tense.",
    "Going forward, lead with the risk.",
    "From now on, put the summary first.",
    "From now on I want plain text.",
    "That is all.\n\nGoing forward, always keep the label.",
    "That is all\n\n\nGoing forward, always keep the label.",
    "From now on, all release notes should be plain text.",
    "Going forward the service must retry.",
    "From now on we should use tabs.",
    "From now on must use tabs.",
])
def test_forward_rule_covers_how_a_new_rule_is_said(text):
    detection = detect_explicit_feedback(text)
    assert (detection.route, detection.marker) == ("standard", "forward")


@pytest.mark.parametrize("text", [
    "> Use the vendor template.\n\nNo, keep the old one.",
    "> Use the vendor template.\nWrong, keep the old one.",
    "Earlier you wrote:\n> The rollout is complete.\nNo, it has not shipped.",
    "> It is ready.\n\nIncorrect, the second table is stale.",
    'Look at this <pasted_content id="1">text</pasted_content> No, keep the old one.',
    "   No, keep the old one.",
])
def test_a_reply_to_a_quote_is_still_a_correction(text):
    assert detect_explicit_feedback(text).marker == "direct"


@pytest.mark.parametrize("text", [
    "> Earlier text.\nIt should be a table.",
    "It should be a table.\n> Earlier text.",
])
def test_a_short_reply_beside_a_quote_is_still_a_reply(text):
    assert detect_explicit_feedback(text, prior_assistant_output="Draft.").marker == "short_should"


@pytest.mark.parametrize("text", [
    "The reply said:\nNo, we cannot do that.",
    "The reply said:\n\nWrong, that is not possible.",
])
def test_a_line_a_colon_introduces_is_not_the_operator_replying(text):
    assert _fires(text) is False


@pytest.mark.parametrize("text", [
    "Why do you think it should be plain text?",
    "Why does the test fail when the result should be 3?",
    "Why is the build red when the version should be 3?",
    "Why do we think the default should be off?",
    "Why would it return null if the value should be set?",
])
def test_real_questions_are_not_why_should_corrections(text):
    assert _fires(text) is False


@pytest.mark.parametrize("text", [
    "You need to keep the source label.",
    "You need to use the smaller card.",
    "The intro needs to be shorter.",
    "Your summary needs to name the source.",
    "Each card needs to show the date.",
    "They need to be in date order.",
    "Every card needs to show the date.",
    "The cards need to be in date order.",
])
def test_instructions_to_the_assistant_and_about_the_work_are_standards(text):
    assert detect_explicit_feedback(text).route == "standard"


@pytest.mark.parametrize("text", [
    "Does this need to be async?",
    "Why does this need to exist?",
    "Where does that need to go?",
    "If this needs to change, tell me.",
    "Let me know if that needs to happen first.",
    "Do you need to rerun the tests?",
    "Does the intro need to be shorter?",
    "Check whether it needs to change.",
    "Tell me when it needs to ship.",
])
def test_asking_about_a_need_is_not_a_standard(text):
    assert _fires(text) is False


@pytest.mark.parametrize("text", [
    "Release notes\n=============\n\nThis is wrong, never use the vendor template.",
    "Your summary line reads ==== and that is wrong.",
    "Summary\n=====\nDon't use the old label",
    "Review notes\n====\nNo, change it back to the short label.\n",
    "x ==== y don't use this form",
])
def test_a_lone_run_of_equals_signs_hides_nothing(text):
    assert _fires(text) is True
    assert strip_delimited_pastes(text) == text.strip()


def test_a_merge_conflict_paste_does_not_hide_the_reply_under_it():
    assert _fires("<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> main\n\nThis is wrong, keep the HEAD side.") is True


@pytest.mark.parametrize("text", [
    "From the other thread:\n======\nYou must always use the vendor template.",
    "From the other thread: ====== You must always use the vendor template.",
    "From the other thread:\n\n======\nYou must always use the vendor template.",
])
def test_a_lone_run_a_colon_introduces_is_a_paste_to_the_end(text):
    assert _fires(text) is False
    assert strip_delimited_pastes(text) == "From the other thread:"


def test_a_colon_before_two_runs_ends_the_paste_at_the_second_run():
    text = "From the other thread:\n======\nA long report.\n======\nThis is wrong."
    assert _fires(text) is True
    assert strip_delimited_pastes(text) == "From the other thread:\nThis is wrong."


@pytest.mark.parametrize("text", [
    "The client wrote 'never use the vendor template' in their note.",
    "The doc says:\n```\nnever use the vendor template\n```\nWhat does it mean?",
    "The doc says:\n```text\nnever use the vendor template\n```\nWhat does it mean?",
    "The doc says:\n~~~\nnever use the vendor template\n~~~\nWhat does it mean?",
    "The doc says:\n```\nnever use the vendor template",
    "The vendor wrote \"You must always\nuse the vendor template\" in the note.",
    "The vendor wrote \u201cYou must always\nuse the vendor template\u201d in the note.",
    "The vendor wrote \"You must always\nuse the vendor template\n\" in the note.",
    "The vendor sent this.\n---\nYou must always use the vendor template.\n---\nThoughts?",
    "The vendor sent this:\n---\nYou must always use the vendor template.",
])
def test_quoted_and_delimited_shapes_are_not_the_operator_speaking(text):
    assert _fires(text) is False


@pytest.mark.parametrize("text", [
    "This is wrong.\n```\nsome quoted code\n```",
    "```\nsome quoted code\n```\nNo, keep the old one.",
    "It's wrong, don't use the old template.",
    "The customers' report is wrong, don't use it.",
    "Title\n---\nNever use the vendor template.",
    "A 5\" screen is fine.\n\nNever use the vendor template.",
])
def test_words_outside_a_quoted_shape_still_count(text):
    assert _fires(text) is True


def test_a_quotation_across_a_blank_line_is_not_one_quotation():
    assert _fires('The label "old\n\nNever use the vendor template for this, says "me"') is True


@pytest.mark.parametrize("text", [
    "Ultrathink:\nNo, keep the source label in the summary.",
    "Ultrathink:\nWrong, keep the source label in the summary.",
    f"{REMINDER}\n\nUltrathink:\nNo, keep the source label in the summary.",
])
def test_one_effort_prefix_is_removed_before_detection(text):
    assert detect_explicit_feedback(text).marker == "direct"


def test_only_one_effort_prefix_is_removed_before_detection():
    assert _fires("Ultrathink:\nUltrathink:\nNo, keep the source label.") is False


@pytest.mark.parametrize("line", [
    "[Image #1]",
    "[Image 2]",
    "[File notes.md]",
    "  [File: notes.md; ref=file_1]  ",
    '[Attached file "notes.md" is saved at: /synthetic/notes.md]',
    '[Attached image "shot.png" is saved at: /synthetic/shot.png]',
])
def test_a_line_that_is_one_attachment_bracket_is_removed(line):
    assert strip_embedded_host_content(f"{line}\nNo, keep the label.") == "No, keep the label."


@pytest.mark.parametrize("text", [
    "[Image #1] looks good, ship [Image #1]",
    "[Image 1] don't use this [Image 2]",
    "[File notes.md] is wrong, change it to match [File plan.md]",
    "No, [Image #1] is wrong, use [Image #2]\n[File notes.md] should say Q3 not Q4 [see line 2]",
])
def test_a_line_that_goes_on_after_an_attachment_bracket_is_kept(text):
    assert strip_embedded_host_content(text) == text
    assert _fires(text) is True


def test_a_paste_ends_at_its_close_tag_with_or_without_an_id():
    bare = '<pasted_content id="1">Never use it.</pasted_content>'
    with_id = '<pasted_content id="ab12">\nNever use it.\n</pasted_content id="ab12">\n'
    assert strip_embedded_host_content(f"Before.\n{bare}\nAfter.") == "Before.\n\nAfter."
    assert strip_embedded_host_content(f"Before.\n{with_id}After.") == "Before.\n\nAfter."
    assert strip_embedded_host_content("<PASTED_CONTENT>Never use it.</Pasted_Content>After.") == "After."


def test_a_tag_that_never_closes_stays_in_the_text():
    text = "The <t3_context> tag is never closed here."
    assert strip_embedded_host_content(text) == text


def test_a_stray_closer_before_an_opener_closes_nothing():
    text = "</pasted_content> then <pasted_content>Never use it."
    assert strip_embedded_host_content(text) == text


def test_a_paste_inside_the_t3_block_goes_with_it():
    text = 'Before.<t3_context><pasted_content id="1">x</pasted_content></t3_context>After.'
    assert strip_embedded_host_content(text) == "Before.After."


def test_a_paste_that_mentions_a_t3_tag_is_still_one_paste():
    text = 'Keep this.<pasted_content id="1">Never use <t3_context> here.</pasted_content>Keep that.</t3_context>'
    assert strip_embedded_host_content(text) == "Keep this.Keep that.</t3_context>"


def test_operator_words_removes_what_the_host_put_around_them():
    text = f'{REMINDER}\n\nUltrathink:\nNo, keep it.\n<t3_context version="1">x</t3_context>\n[Image #1]'
    assert operator_words(text) == "No, keep it."


LESSON = "Never use the vendor template for release notes."


@pytest.mark.parametrize("text, sentences", [
    (f"This looks good overall. {LESSON}", ["This looks good overall.", LESSON]),
    ("I approved the earlier plan on Monday. You must keep the source label on every card.",
     ["I approved the earlier plan on Monday.", "You must keep the source label on every card."]),
    ("No. Use the smaller card instead of the wide one on every page.",
     ["No.", "Use the smaller card instead of the wide one on every page."]),
    ("Do not do it. Background text goes here for a while. Do not use the vendor template for release notes.",
     ["Do not do it.", "Do not use the vendor template for release notes."]),
    ("In src/imprint/cli.py you must keep the hook order exactly as it is.", None),
    ("Never change settings.json by hand, always use the installer CLI.", None),
    ("We must pin python 3.14.7 on every worker before the next release.", None),
    ('Always keep the draft "as is" unless the client asks for a change in writing.', None),
    ('Never use "vendor" templates for any of the release notes we send.', None),
    ("Change the rate to 3.5 percent and do not use 3.25 percent anywhere in the deck.", None),
    ('You must never ship without "legal review" on anything client facing.', None),
    ('We must keep everything except "pricing" in the client deck.', None),
    (f"An opening line.\nThis looks good so far, thanks.\n{LESSON}\nA closing line.",
     ["This looks good so far, thanks.", LESSON]),
    ("A plain line of background.\nNo, that one is the wrong card for this.",
     ["No, that one is the wrong card for this."]),
    ("A plain line of background.\n\nNo, that one is the wrong card for this.",
     ["No, that one is the wrong card for this."]),
    ("A plain line of background. Wrong, that is the wrong card for this.",
     ["Wrong, that is the wrong card for this."]),
    ("   No, that one is the wrong card for this.", ["No, that one is the wrong card for this."]),
])
def test_feedback_sentences_returns_whole_lessons_in_order(text, sentences):
    assert feedback_sentences(text) == (sentences or [text])


def test_a_later_sentence_keeps_what_it_says_about_an_earlier_one():
    text = "Legal told us we must always ship on Fridays. That is wrong. Ship on Tuesdays."
    assert feedback_sentences(text) == ["Legal told us we must always ship on Fridays.", "That is wrong."]


def test_rule_order_does_not_decide_the_order_of_sentences():
    text = "Wrong, that is the wrong card for this. Later on, this looks good to me."
    assert feedback_sentences(text) == ["Wrong, that is the wrong card for this.", "Later on, this looks good to me."]


@pytest.mark.parametrize("text", [
    "But the pricing table is wrong, remove the third tier.",
    "However, the third tier has to go.",
    "Replace the chart with the table.",
    "The pricing table looks wrong to me.",
    "What is wrong with the build today?",
])
def test_words_that_choose_a_sentence_do_not_capture_a_message(text):
    assert feedback_sentences(text) == [text]
    assert detect_explicit_feedback(text).is_feedback is False


def test_a_sentence_two_rules_match_is_returned_once():
    assert feedback_sentences(f"{LESSON} Never use it, that is wrong.") == [LESSON, "Never use it, that is wrong."]
    assert feedback_sentences("No, never use the vendor template, that is wrong.") == [
        "No, never use the vendor template, that is wrong.",
    ]


@pytest.mark.parametrize("text", [
    'Never "the vendor one" use the vendor template for release notes.',
    "Do not\n> quoted line\nchange the release note template at all.",
    'Do not "really" use the vendor template for release notes.',
    f'{REMINDER}\n\nDo not "really" use the template.',
    'Ultrathink:\nDo not "really" use the template.',
    f'A line.\n\n<pasted_content id="1">Never use it.</pasted_content>\n\n{LESSON}',
    f"Notes:\n======\nYou must ship on Friday.\n======\n{LESSON}",
    f"A line.\n```\nYou must ship on Friday.\n```\n{LESSON}",
    f"> You must ship on Friday.\n{LESSON}",
    "Do not ship it. " * 200 + LESSON,
])
def test_feedback_sentences_are_always_exact_parts_of_the_text(text):
    assert all(sentence in text for sentence in feedback_sentences(text))


@pytest.mark.parametrize("text", [
    "Do not\n> quoted line\nchange the release note template at all.",
    "Do not\n======\nquoted\n======\nchange the release note template at all.",
    "Plain background with nothing to act on, at some length.",
    "Short. Lines. Only.",
    "No. Wrong.",
    "This is not what I asked for at all. Background text goes here for you to use.",
])
def test_feedback_sentences_returns_nothing_rather_than_a_stitched_line(text):
    assert feedback_sentences(text) == []


@pytest.mark.parametrize("text", [
    f"{REMINDER}\n\nNo, keep the failed source in the summary.",
    "<system-reminder>IMPORTANT: you must follow them exactly as written.</system-reminder>\n\n"
    "No, keep the failed source in the summary.",
    "<task-notification>Worker done: this looks good overall and was approved by CI.</task-notification>\n"
    "No, keep the failed source in the summary.",
    "Ultrathink:\nNo, keep the failed source in the summary.",
])
def test_feedback_sentences_never_returns_what_the_host_prepended(text):
    assert feedback_sentences(text) == ["No, keep the failed source in the summary."]


def test_a_delimited_span_in_a_line_does_not_join_the_sentences_around_it():
    text = "Keep the source label on it ==== You must ship on Friday. ==== Never use the vendor template."
    assert feedback_sentences(text) == ["Never use the vendor template."]
