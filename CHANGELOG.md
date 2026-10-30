# Changelog

## Unreleased

- Add an optional, fail-open Jev (TypeSafe System One) relevance selector for
  prompt-time retrieval. The default `selector.mode` stays `deterministic` with
  no network request; `jev` and `shadow` are opt-in, `IMPRINT_SELECTOR=off`
  forces deterministic retrieval, egress is limited to the prompt (plus an
  optional previous-reply excerpt) and short judgment statements (see
  `docs/privacy-and-recovery.md`), selections never exceed the byte budget,
  never-sent domain records are still delivered locally, every failure
  returns exactly the deterministic behavior, decisions are logged without
  content, and health reports the selector's network state truthfully.

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
