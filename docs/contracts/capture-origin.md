# Capture origin

Automated callers set the process environment variable IMPRINT_CAPTURE_ORIGIN=automation before starting the host CLI. The hook bridge preserves it. Stop capture returns a content-free machine_origin skip before opening an operator store, even if an event supplies operator_text or claims human origin. Unknown nonempty transport markers also skip as unverified. This is a cooperative provenance contract, not authentication against a malicious local caller.

Without a machine marker, transcript origin.kind=human is explicit human authorship. The host interactive prompt sources typed, queued and suggestion_accepted are accepted when origin is absent. SDK and unknown prompt sources with no explicit human origin return unverified_provenance and cannot create operator canon. Headless human submission remains supported with explicit human transcript origin, or the existing direct operator_text hook API from a human caller. Do not set an automation marker for that human submission.

Human text quoting automation is still human: text does not establish transport identity. Existing structural checks and legacy diagnostic markers remain; malformed complete transcript lines still fail closed. Both normal and bounded-tail transcript parsers apply the same gate. Existing canon is not relabeled or deleted.

Known automated producers: the Astra–Fable peer wrapper and Fully Aware daily scan, nightly fix, and taste distiller. Deploy producer markers and this capture gate together; older installed Imprint versions ignore the environment marker. Roll back them together if necessary.

## T3 threads

T3 submits every prompt through the SDK, so its transcript entries carry `entrypoint=sdk-ts` and `promptSource=sdk` whether a person or an agent wrote them. They stay unverified by default.

With `t3_capture_origin: true`, an unverified `sdk-ts` entry from a run with `T3CODE_THREAD_ID` set is checked against T3's own records, read-only, from `<T3CODE_HOME or ~/.t3>/userdata/statev2.sqlite`. T3's command line reports authorship per thread, not per message, so those records are the only per-message source today. The entry counts as human only when all of these hold:

- **Session.** A row in `orchestration_v2_projection_provider_threads` for that thread has `nativeThreadRef.nativeId` equal to the Stop event's `session_id`, compared as an exact string. Every job an agent starts inherits `T3CODE_THREAD_ID`, so without this a nested run could match a person's words. No such row, a missing field or any other shape leaves the entry unverified.
- **Newest match.** Among the thread's newest 64 prompts, the newest with the same words decides, so an agent repeating a person's words is judged as the agent.
- **Author.** That prompt has `createdBy=user`, a string `creationSource` of `web` or `mobile`, and a message id.
- **Not a machine or a third party.** It has no truthy `senderThreadId`, `notification`, `scheduledTaskId` or `context.guest`, and a `context` of any other shape refuses it. Scheduled and webhook prompts keep `user` and `web`; so does a message sent for a guest.
- **Not a plan.** Its text does not begin with `PLEASE IMPLEMENT THIS PLAN:\n`, after removing one leading `Ultrathink:\n`. T3's Implement button sends the assistant's plan that way and no field marks it.

"The same words" is compared after removing, from both sides, leading `<system-reminder>` and `<task-notification>` blocks, one `Ultrathink:\n` prefix, `<pasted_content>` and `<t3_context>` blocks, and attachment lines. A stored context chip `[label](t3-context://v1/kind/id)`, with `!` before an image, is compared in the form T3 sends the provider, `[Kind: label; ref=id]`. Inside one session the match is still by words, because T3 gives the provider a random prompt id rather than the message id. A prompt T3 sends in any other form stays unverified, including an image sent with captured-window data.

The record settles only what the entry left unverified. A declared origin, a structural refusal and `IMPRINT_CAPTURE_ORIGIN=automation` all stand. A capture whose verdict basis is `host_record`, and no other, carries an `org.imprint.host_origin` extension with `host`, `basis`, `message_id`, `created_by` and `creation_source`. The parsers return the basis, so a turn decided by its own origin or prompt source is never stamped.

Both parsers use only the newest prompt. An unverified prompt newer than the last operator entry ends the search and returns the unverified skip, so an older turn is not read again once its capture mark has expired. A tool result is not a prompt and does not end the search. If a person's prompt and an agent's both arrive before any Stop, the person's turn is skipped.

A T3 record that cannot be read is not evidence about the prompt. A missing store, a missing table or column, a locked store, a stored payload that is not valid JSON, or a message payload that is not an object all leave the entry unverified. The Stop receipt then gives `t3_record_unavailable` instead of `unverified_provenance`, the skip is logged to `logs/capture-skips.jsonl`, and `imprint health` reports `t3_record_unavailable` as a degraded reason for 24 hours, with `t3_record_unavailable_24h` and `t3_record_unavailable_newest_age_seconds` counted from the newest 64 KiB of that log. The turn itself is not retried. A provider row that decodes but names another session, or has another shape, is an ordinary `unverified_provenance`.

Someone else's words are kept out of the stored text where they can be told apart. On every host, `<pasted_content>` and `<t3_context>` blocks, attachment lines, `>` block quotes, and text between two runs of four or more equals signs or from a lone run to the end of the message are removed before detection and before the words are stored. For a capture attested by T3, leading host blocks and the `Ultrathink:\n` prefix are removed as well. A quotation inside a sentence, in double or curly quotes on one line, is ignored by the detector but stays in the stored text as part of that sentence. Code fences, single quotes, `---` blocks and quotations spanning lines are not recognised, and count as the operator's words.

A transcript that does not exist at Stop is skipped and logged to `logs/capture-skips.jsonl`, because blocking the stop cannot make the host write it. A symlink or non-regular file is still refused.
