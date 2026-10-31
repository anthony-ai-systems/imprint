# Changelog

## Unreleased

- Deliver a Jev pick too large to share the budget as a compact line: the
  record's own opening words, its authority and source references, and how to
  read the whole record locally. The canonical record is unchanged.
- Report `oversized_picks` when Jev picked records that still cannot fit, which
  was reported as `no_picks`, and log content-free counts per stage and the
  request size with each selector decision.
- Add optional verification of T3 prompts against T3's own message record
  (`t3_capture_origin`, default off). T3 submits every prompt through the SDK,
  so its prompts stayed unverified and no correction made in T3 was captured.
  Only a prompt T3 records as written by a person at a client counts; anything
  else stays unverified, and declared origins and machine markers still win.
- Stop splitting transcript lines on U+2028, U+2029 and U+0085, which are legal
  inside a JSON string and were reported as malformed complete lines.
- Skip a transcript that does not exist at Stop instead of blocking the stop,
  and log the skip content-free to `logs/capture-skips.jsonl`. Symlinks and
  non-regular files are still refused.
- Keep pasted text out of capture: `<pasted_content>` and `<t3_context>` blocks
  and attachment lines are removed before detection and storage, and the
  detector ignores quoted spans and text pasted between `====` runs.
- Detect `From now on ...` as a standard, and short `It should be ...` and
  `Why ... should be ...` replies as corrections after an assistant turn. A bare
  `need to` no longer counts as a standard; its subject must be the work itself.

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
