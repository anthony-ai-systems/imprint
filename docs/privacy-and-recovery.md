# Privacy, Cost, and Recovery

## Data boundary

Imprint stores captured words, nearby case context, alternatives, provenance,
and derived records on the local machine. That material can reveal strategy,
preferences, clients, confidential work, or personal information. Treat the data
root, SQLite database, spool, projections, JSON-LD exports, and settings backups
as sensitive.

The core release sends no telemetry and makes no network request. It has no model
dependency. If a future or optional adapter is enabled, its provider receives the
content explicitly sent to it and may charge usage fees under that provider's
terms. No optional adapter is enabled by installation.

### Optional Jev relevance selector

The selector is off by default (`selector.mode: "deterministic"`). Only when an
installation sets `selector.mode` to `jev` or `shadow` does Imprint send one
HTTPS `POST` per non-empty prompt to the configured endpoint
(`https://api.typesafe.ai/v1/systemone` by default). Two paths send it: the
UserPromptSubmit hook, and `imprint retrieve --prompt ...` run by hand or by a
script. Exactly this leaves the machine:

- the request body keys `model`, `state`, and `questions`, nothing else;
- `state`: the prompt text, at most 8,000 characters, as written: unlike
  capture, it keeps pasted text and host context blocks. If
  `short_prompt_context` is enabled and the prompt is short, `state` also
  contains up to `max_chars` of the previous assistant reply, which can quote
  anything the assistant wrote (code, file names, people, figures);
- `questions`: at most `prefilter_keep` (default 150) entries named `c0`,
  `c1`, ... Each asks "Does this judgment apply to the current turn?" and
  quotes one judgment statement, at most 400 characters: the operator's own
  Verdict words, or the statement of a Principle, Belief, Value, Rule, or
  Pattern. A statement is reduced to the operator's words first, so a record
  stored by an older build does not send host blocks, pasted text, attachment
  lines, chip payloads or quoted assistant text. Everything else in it is sent
  as written, so it can contain whatever the operator wrote. With
  `stale_before` set, stop-hook captures from before that time that no longer
  read as feedback are not sent. With `standing_rules` on, up to `keep` rules are
  sent on every turn, whatever the prompt says;
- the key in the `Authorization: Bearer` header, plus `Content-Type` and an
  `imprint-local/<version>` `User-Agent`.

Never sent by Imprint itself (the prompt and the optional reply excerpt are
sent as written, and each statement as described above, with whatever they
contain): record, case,
evidence, source-receipt, or session ids (Imprint URNs inside statements are
replaced with `[ref]`); case summaries; ontology paths; domain ids; the hook's
working directory and transcript path; referred parties, names, amounts,
prices, and metrics from business, observation, or outcome records; and
self-model, chosen/default future, ingested, and consent records.

The endpoint operator (TypeSafe, or OpenRouter if configured) processes that
content and may log, retain, or bill for it under its own terms. Review the
provider's data-use and retention terms before enabling the selector, and keep
`exclude_domains` or a narrower `node_types` for judgments that must not leave
the machine. Those records are still delivered locally when their domain
matches. Both settings limit only judgment statements: the prompt is sent even
when it matches an excluded domain, so use `IMPRINT_SELECTOR=off` for work
whose prompts must stay local. `IMPRINT_SELECTOR=off` (or any non-empty value
other than `jev` or `shadow`) disables it immediately. The local decision log
(`logs/jev-selector.jsonl`) records only ids, scores, timings, outcomes, and
content-free counts (request size, records and bytes per delivery stage), never
prompt or judgment text; `logs/jev-selector-transfer.json` holds only the last
transfer and success timestamps.

On POSIX the optional `api_key_file` must be a private regular file owned by
the current user. On Windows, Imprint checks only that it is a regular file,
not its owner or ACL; there, prefer the key environment variable or keep the
file in a directory only your account can read.

Explicit local judgment capture is the only consent-exempt source. Transcripts,
conversation imports, Screenpipe, financial records, behavioral telemetry,
business systems, customer results, and external connectors are denied by
default. A typed `ConsentGrant` must authorize the source, purpose, operation,
sensitivity, retention rule, and effective interval before an Observation or
Outcome can enter the canonical store. Revocation and expiry are evaluated at
write time; they are not configuration comments.

## Permissions and disk protection

Use a private OS account and full-disk encryption. Do not put the data root in a
public repository, support ticket, cloud-sync folder, or shared network drive.
The installer does not change broad directory permissions or grant another user
access. Your OS defaults still apply; inspect them if the machine is shared.

## Backup

Create and verify a transactionally consistent backup with:

```bash
imprint backup create
imprint backup verify /path/from/create.sqlite3
```

The command uses SQLite's backup interface and emits a tamper-evident receipt.
For a whole-directory disaster-recovery copy, stop active sessions and the
compiler, then copy the complete operator directory to encrypted storage.

For a portable logical snapshot:

```bash
imprint export --format jsonld --output imprint-export.jsonld
```

Protect exports like the live bank. Before upgrades, make a physical backup and
record its SHA-256. Additive migrations preserve historical versions, but a
backup is the recovery boundary for hardware loss or operator error.

## Recovery

1. Stop all writers.
2. Preserve the damaged directory before attempting repair.
3. Restore the entire last known-good operator directory to a local path.
4. Point `data_root` at its parent and run `imprint health`.
5. Compare expected counts and hashes before resuming capture.

To roll back to an older build, remove `t3_capture_origin` from the config
before reinstalling it. An older build refuses a config with that key: its
installer stops and leaves the newer build in place, and on the older code every
hook and `imprint version` fail. Set the key again only after a build that knows
it is installed.

Never merge two SQLite files. Reconcile immutable spool inputs through one
compiler instead. Quarantined or corrupt inputs should remain preserved for
inspection; do not silently discard them.

Compiler acknowledgement does not delete source input. To prune this producer's
old committed inputs after the configured retention period, run `imprint spool
prune`. An input remains untouched if its acknowledgement, event identity, hash,
producer path, or retention age cannot be verified.

## Uninstall and deletion

Uninstall deliberately preserves captured data. Canonical deletion is a separate
two-step operation:

```bash
imprint delete purge --scope EXACT_NODE_OPERATOR_SESSION_OR_SOURCE_ID --preview
imprint delete purge --scope EXACT_NODE_OPERATOR_SESSION_OR_SOURCE_ID \
  --confirm EXACT_NODE_OPERATOR_SESSION_OR_SOURCE_ID
```

The second command is irreversible. It records only non-content counts, rebuilds
projections, scans the active root, and reports `purged_with_residue` with a
nonzero exit if deletion committed but content remains. Backups and exports
outside the active root are not discoverable and must be inventoried and deleted
separately. Tombstone is the normal reversible removal from current retrieval.

Revoking consent does not silently rewrite historical evidence. If a grant uses
`delete_on_revoke`, the associated source IDs must be purged through the same
explicit preview-and-confirm deletion workflow so backups, exports, and residue
can be reported honestly.
