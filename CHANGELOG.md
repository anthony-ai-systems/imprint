# Changelog

## Unreleased

- Add `selector.jev.standing_rules`, off by default. A rule set with "from now
  on", "always", "never" or similar is always sent to Jev, and takes a reserved
  place ahead of other picks when Jev scores it at 0.25 or more and it shares a
  rare word with the prompt. Without this, a correction reached a later session
  only while it was the newest record.
- Add `selector.jev.drop_stale_records`, off by default, which keeps captured
  records that no longer read as feedback out of the request and local delivery.
  No record is changed.
- Read each statement once per turn, which more than pays for the checks above.
- Deliver a Jev pick that cannot share the budget as a compact line: the
  record's text when it fits 400 bytes, otherwise every sentence that reads as
  feedback in document order, otherwise its opening, with its authority and
  source references and how to read the whole record locally. The canonical
  record is unchanged. Picks that all fit stay whole.
- A pick compacted for want of room is not latched, so a later turn or the
  deterministic fallback can deliver it whole. Only a pick that can never fit
  whole counts as delivered from its compact line.
- Report `oversized_picks` when Jev picked records that cannot fit, or
  `crowded_picks` when local records took the room, instead of `no_picks`. Log
  the delivery outcome beside Jev's own, content-free counts per stage including
  local records and bytes, and the request size with each selector decision.
  Both the delivery outcome and the stages are null on shadow and fallback lines.
- Add optional verification of T3 prompts against T3's own message record
  (`t3_capture_origin`, default off). T3 submits every prompt through the SDK,
  so its prompts stayed unverified and no correction made in T3 was captured.
  Only a prompt T3 records as written by a person at a client counts; anything
  else stays unverified, and declared origins and machine markers still win.
- Tie T3 verification to the Claude session: T3's provider-thread record must
  name the Stop event's session, so a nested run that inherits the thread cannot
  match a person's words. Refuse scheduled and webhook prompts, messages sent for
  a guest, and plans sent by T3's Implement button, all of which T3 records as
  `user` and `web`.
- Report an unreadable T3 record as `t3_record_unavailable` in the Stop receipt,
  in `logs/capture-skips.jsonl` and in `imprint health`, instead of an ordinary
  unverified skip.
- Match T3 prompts that carry context chips, images or a leading host block, and
  keep T3's `Ultrathink:` prefix out of detection and stored text.
- Stamp `org.imprint.host_origin` only on captures decided by the T3 record, and
  name the T3 message in it.
- End the search at an unverified prompt newer than the last operator turn,
  instead of re-reading the older turn.
- Stop splitting transcript lines on U+2028, U+2029 and U+0085, which are legal
  inside a JSON string and were reported as malformed complete lines.
- Skip a transcript that does not exist, is empty, or disappears during the read
  at Stop instead of blocking the stop, and log the skip content-free to
  `logs/capture-skips.jsonl`. Symlinks and non-regular files are still refused.
- Keep at most 64 KiB of the prior assistant message on both parsers and record
  the cut, so a correction after a very long reply is captured. Skip operator
  text over 256 KiB as `operator_text_oversized` instead of failing the Stop hook;
  it is never cut.
- Skip blank transcript lines in the full parser as the tail parser does, skip
  lines of valid JSON too deep or too long to read, and replace lone surrogates,
  so unusual input no longer crashes a hook. A malformed line is still refused.
- Record a hook timeout in `logs/hook-failures`, so `imprint health` reports it.
- Keep pasted text out of capture: `<pasted_content>` blocks in the form Claude
  Code writes them, `<t3_context>` blocks, lines that are one attachment bracket,
  `>` block quotes and text pasted between `====` runs are removed before
  detection and storage. A lone `====` run hides nothing unless a colon introduces
  it. The detector also ignores double, curly and single quotations, code fences
  and `---` blocks, which stay in stored text.
- Detect a new standing rule (`From now on use ...`, `Thanks, going forward keep
  ...`) as a standard, but not a question, with or without its question mark, a
  description of what will happen or a line a colon introduces. Detect short replies that name a target (`It should be
  a table.`) and `Why ... should be ...` statements as corrections after an
  assistant turn, but not closing remarks (`That should be enough.`) or real
  questions. `need to` counts only with the work or the assistant as its subject,
  and not in a question or an `if` clause.
- Detect a correction that replies to a quoted line, and one that follows T3's
  `Ultrathink:` prefix.
- Stop splitting file names and version numbers when choosing a compact line's
  sentences, and never show text a host prepended as the operator's. Each sentence
  is an exact part of the record.
- Bound the detector's patterns so long blank runs, unclosed tags and unclosed
  quotes no longer stall a hook past its time limit.
- Keep the assistant's words out of a correction that replies to a T3 quote: an
  `[Assistant quote]` link is removed, its comment is kept and read as the
  operator's, and an all-quote message stores nothing.
- Read a prompt sent while a turn runs, which Claude records as a `queued_command`
  attachment, where a host record can judge it.
- Match T3 prompts sent after a handoff, or with captured-window data, a truncation
  note, or a `]` in an attachment name. Store a context chip as its label and stop
  a chip's label firing the detector.
- Report T3 join failures by name: `t3_session_ref_unreadable`, which degrades
  health, and `t3_session_unmatched` and `t3_prompt_unmatched`, which are counted.
  Add `capture_skips_24h` to `imprint health`, covering those, oversized text and
  the transcript skips.
  The skip log keeps one older copy past 4 MiB, and the counts are of Stop events.
- Stop blocking a Stop on what cannot heal: a refused transcript now returns
  `transcript_refused`, exit 0, is logged and degrades health. Nothing more is read
  from a refused file. A non-object event and a too-deep event no longer fail the
  hook. A bad config, a final line still being written and a file replaced during
  the read still block once.
- Over 16 MiB, look back the whole 16 MiB for a prompt before giving up, and report
  `transcript_tail_exhausted` rather than a missing prompt.
- Skip an operator turn older than the capture marks as `operator_turn_expired`,
  and key marks on the transcript when the event has no session id, so a turn is
  not captured twice.
- Read paste blocks by their `id`, nested tags, attributes of any length and an
  unclosed opener. Keep leading host blocks out of stored text on every host.
- Detect a mid-message reversal, a plain `Always ...` or `Never ...` rule, a
  standing rule after a clause, and short rejections, and stop reading an ordinary
  question as a verdict.
- Show a compact line's closing sentence, fill its sentences backward from the
  last, cut an oversized last sentence rather than lose it, keep abbreviations
  whole, and never produce an empty line.
- Send a remote selector only the operator's words from a statement, including one
  stored by an older build.
- Tolerate a T3 home that cannot be resolved, a newer T3 row with no string text,
  and a FIFO at the skip log, and replace a lone surrogate on both sides of the T3
  comparison.
- Rollback: `t3_capture_origin` is unknown to older builds, which refuse the config
  and fail every hook. Set it only after this build is installed, and remove
  `t3_capture_origin` from the config before reinstalling an older build.

- Add an optional, fail-open Jev (TypeSafe System One) relevance selector for
  prompt-time retrieval. The default `selector.mode` stays `deterministic` with
  no network request; `jev` and `shadow` are opt-in, `IMPRINT_SELECTOR=off`
  forces deterministic retrieval, egress is limited to the prompt (plus an
  optional previous-reply excerpt) and short judgment statements (see
  `docs/privacy-and-recovery.md`), selections never exceed the byte budget,
  never-sent records are still delivered locally, every failure returns
  the deterministic behavior minus records Jev already delivered to the
  session, decisions are logged without
  content, and health reports the selector's network state truthfully.
- Add `imprint ratify apply`: apply a ratification card's recorded per-item answers to a typed-knowledge batch packet, pin ratified rules in the owner's scope-bindings manifest, and prove them through the native judgment reader. Re-runs are idempotent.

## 3.0.1 — 2026-07-15

- Close JSON-LD import authority smuggling and preserve non-mutating dry-runs.
- Automatically compile explicit Stop-hook feedback on the authorized writer.
- Add owned cross-platform launchers and bounded installed hook bridges.
- Enforce strict configuration types before authority decisions.
- Enforce private local storage permissions and restricted Windows ACLs.
- Make health evidence inspect real queues, backups, permissions, and activity.
- Preserve opaque stable Claude session lineage without raw provider IDs.
- Make once-delivery retrieval crash-recoverable.
- Use compiler heartbeat and local process liveness for lease recovery.
- Refuse incompatible stores before DDL or ordinary writes.
- Preserve bounded feedback evidence from enormous transcripts.
- Refuse ambiguous WAL state and unsupported ontology versions before canonical writes.
- Validate backups before replacement and restore the prior live database on failed restore.
- Replay prepared retrieval after pre-output crashes and commit only after flushed delivery.
- Recover stale compiler locks conservatively and require exact canonical acknowledgements.
- Support verified in-place upgrades from 3.0.0 while preserving data and external state.
- Verify embedded release provenance, source revision, source digest, and archive equivalence.

## 3.0.0 — 2026-07-15

- Rebuilt Imprint around raw Case/Verdict evidence, mandatory provenance, and bitemporal history.
- Added a local SQLite canonical store, immutable per-node spool, bounded retrieval, JSON-LD export, ingestion floor, and explicit deletion contracts.
- Added portable installers, uninstaller, release validators, CI, and artifact acceptance.
- Kept passive observation and periodic summarization out of shipped claims.
