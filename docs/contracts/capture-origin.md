# Capture origin

Automated callers set the process environment variable IMPRINT_CAPTURE_ORIGIN=automation before starting the host CLI. The hook bridge preserves it. Stop capture returns a content-free machine_origin skip before opening an operator store, even if an event supplies operator_text or claims human origin. Unknown nonempty transport markers also skip as unverified. This is a cooperative provenance contract, not authentication against a malicious local caller.

Without a machine marker, transcript origin.kind=human is explicit human authorship. The host interactive prompt sources typed, queued and suggestion_accepted are accepted when origin is absent. SDK and unknown prompt sources with no explicit human origin return unverified_provenance and cannot create operator canon. Headless human submission remains supported with explicit human transcript origin, or the existing direct operator_text hook API from a human caller. Do not set an automation marker for that human submission.

Human text quoting automation is still human: text does not establish transport identity. Existing structural checks and legacy diagnostic markers remain; malformed complete transcript lines still fail closed. Both normal and bounded-tail transcript parsers apply the same gate. Existing canon is not relabeled or deleted.

Known automated producers: the Astra–Fable peer wrapper and Fully Aware daily scan, nightly fix, and taste distiller. Deploy producer markers and this capture gate together; older installed Imprint versions ignore the environment marker. Roll back them together if necessary.

## T3 threads

T3 submits every prompt through the SDK, so its transcript entries carry `entrypoint=sdk-ts` and `promptSource=sdk` whether a person or an agent wrote them. They stay unverified by default.

With `t3_capture_origin: true`, an unverified entry from a run with `T3CODE_THREAD_ID` set is checked against T3's own message record, read-only, from `<T3CODE_HOME or ~/.t3>/userdata/statev2.sqlite`. T3's command line reports authorship per thread, not per message, so that record is the only per-message source today. The entry counts as human only when the newest prompt in that thread with the same words has `createdBy=user`, `creationSource` of `web` or `mobile`, no `senderThreadId` and no `notification`. A missing store, a changed schema, an unreadable record or any other value leaves the entry unverified.

The record settles only what the entry left unverified. A declared origin, a structural refusal and `IMPRINT_CAPTURE_ORIGIN=automation` all stand. Captures made this way carry an `org.imprint.host_origin` extension naming the basis.

T3 carries pasted text and attachment details inside the message. `<pasted_content>` and `<t3_context>` blocks and attachment lines are removed before detection and before the words are stored, and quoted spans are ignored by the detector, so someone else's words are not recorded as the operator's.

A transcript that does not exist at Stop is skipped and logged to `logs/capture-skips.jsonl`, because blocking the stop cannot make the host write it. A symlink or non-regular file is still refused.
