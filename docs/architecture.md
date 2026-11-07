# Architecture and Provenance

Imprint separates witnessing from interpretation:

1. **Recorder:** writes an immutable capture envelope containing raw Case,
   Verdict, Call, nullable Reason, alternatives, evidence hashes, and actor data.
2. **Compiler:** the sole canonical writer validates and appends the envelope to
   SQLite in WAL mode.
3. **Derivation:** proposes Principles, Rules, Patterns, Beliefs, Values, and
   Domains through an immutable proposal spool without rewriting source evidence.
4. **Ratification:** explicit operator action may promote eligible extracted or
   inferred material.
5. **Retrieval:** selects only eligible current records under a deterministic
   byte budget and emits provenance with every item. An installation may opt
   in to a remote relevance selector (Jev) that ranks the same eligible
   judgments at prompt time; it never widens eligibility or the byte budget and
   falls back to deterministic retrieval on any failure.

Nodes and edges carry one of `captured`, `extracted`, `inferred`, or `ratified`.
The store preserves valid time and system time, plus supersession,
contradiction, reversal, and tombstone history. Stable typed URNs and schema
versions make migrations additive and replay idempotent.

Canonical Domains are stable, operator-scoped nodes with evidence-backed add,
select, and freeze transitions. A `contradicts` edge keeps both current heads. A
`supersedes` edge runs from the replacement to the prior head; only the prior
head is closed for current retrieval, while its versions and the transition
remain inspectable.

After canonical commit, the compiler writes a content-free acknowledgement with
the input event ID and exact hashes. `spool prune` may delete only the configured
producer node's inputs after that acknowledgement has aged past the configured
retention period and every hash and path check succeeds.

External documents enter quarantine. `KEEP` requires a WHY and creates an
`imported_floor` record with a source receipt; it cannot become captured operator
judgment. `KILL` remains an auditable ruling. A finished, approved, published, or
frozen deliverable is refused as cold-start evidence.

SQLite is canonical. Markdown is a human-readable projection. JSON-LD is the
lossless portable graph projection. Optional Atlas or Neo4j integrations are
adapters, never required canonical stores.

## Record scope

The `record_scope` table holds at most one row per retrievable record: whose it is
(`client`, `central`, `core_owner`, `unresolved` or `held`), an optional client and
project, a delivery tag and rank, a subject, and any `overrides`. The row is keyed
by node, so a new version keeps it. A record with no row is legacy. A client or
project id has one shape: lower case letters and digits joined by single hyphens,
such as `north-wind`. Python refuses any other by name, and so does the table, which
also refuses `overrides` that are not a JSON list. The delivery tag, rank and
subject are checked in Python only, so their rules can still change. This release
only carries scope: no delivery route reads a row, and with no row and no scope
extension the output matches the previous release byte for byte. The store and
record schema versions do not move, and an older build still opens the store. The
snapshot id moves at the first row, and then whenever a row changes.

- **Capture.** An envelope may carry the `org.imprint.scope` extension, as
  `{schema_version, payload}`. The compiler writes the row in the capture's own
  transaction and quarantines a spool whose stamp is invalid. A stamp cannot say
  `central` and cannot carry `overrides` or a `subject`. Nothing adds the extension
  yet, so the Stop hook, `imprint capture` and a `CapturePipeline.capture` caller
  that passes no `extensions` all write legacy records.
- **A `held` stamp** can list `candidate_client_ids`. The list stays on the event,
  where `imprint history` shows it in `event_payload`, and has no column.
- **`said_in`, `channel` and `carrier`** can go on any stamp: where the words were
  said, and how they arrived. Each is null or non-empty text, and nothing checks
  its value. They stay on the capture event, where `imprint history` shows them in
  `event_payload`, and have no column. Nothing stamps them yet. This build knows
  them so that a later build's stamp is not refused after a rollback, and the
  extension's `schema_version` stays `1.0.0`. On the other paths that take a stamp,
  they are checked and kept nowhere, as `candidate_client_ids` is.
- **`imprint scope set`** appends a `scope_changed` event and updates the row in
  one transaction, moving only what it names, and `resolved_by` becomes `explicit`.
  It is the only way out of `held`. It takes `--override` only for a current central
  record, and `scope_changed` is the only event that carries overrides. Because a
  row sits beside the node's versions, a change adds no version. `imprint history`
  lists the event and shows the current row as `scope`, or no `scope` when there is
  no row.
- **Subject.** A row may carry a `subject`: lower case words joined by single
  dots, such as `marketing`, `marketing.video`, `engineering` or `work`. The head
  before the first dot says which work the record is about, and the tail is open.
  Only `imprint scope set --subject` writes one, on a row of any kind, and `--clear
  subject` removes it. A capture stamp and the ontology flags cannot carry one. A
  client row takes no subject, `marketing` or `marketing.<tail>`, so `engineering`,
  `work` or any other head is refused there, and so is a change of kind that would
  bring a row with such a subject to `client`. Nothing reads the subject yet.
- **Other paths.** `ontology add-node`, `observation add` and `outcome add` take
  `--scope-kind`, `--scope-client` and `--scope-project`, and write no row without
  them. `imprint ratify apply` stamps a new node `unresolved`, except a Domain.
  `import_legacy_principles` stamps `unresolved` unless given a scope.
  `imprint ingest keep` writes no row yet.
- **Supersession.** A successor keeps its own row. With none, it takes a copy of the
  target's row in the same event, unless the target is central: a central row is
  never copied. The copy says `resolved_by: inherited`, and its event names the
  record it came from as `target_id`. A successor may close a record only if it
  reaches the same readers: a central record only by a central one, a client record
  only by one of the same client, and a `core_owner` record only by a `core_owner`
  one. Every other pair is refused, so one client's record cannot close another's
  and an unknown record cannot close a central one. A record that is `unresolved`,
  `held` or has no row has no settled readers yet, so any successor may close it. A
  client record adds to a central one through `overrides` and never replaces it.
- **Who writes `central`.** Nothing in this release does. `imprint scope set`, a
  capture stamp and the ontology flags all refuse it, and import accepts it only on
  a ratified record.
- **Retrieval.** `RetrievalRecord.scope` carries the row. Eligibility, ordering and
  rendering ignore it.
- **Delivery contract, for the filter a later release adds.** No row, `unresolved`
  and `held` are unknown, and an unknown record is never treated as `central`. A
  client door receives only `central` records and `client` records with its own
  exact client id. `core_owner` marks a record about the operator's own systems and
  tools rather than any client's work, so no client door receives it: it reaches
  only doors that are not a client's, and what else those doors receive is not
  settled here. The filter is an allow-list, never a list of what to drop: a record
  reaches a door only when its row matches an entry above, so a missing or lost row
  withholds the record, and a door whose own client cannot be resolved receives
  `central` only.

## Semantic ontology contract

The store schema and semantic ontology are versioned independently. The generic
versioned graph remains stable while the `3.1.0` semantic contract closes four
evidence channels:

1. **Imprint judgment:** witnessed Cases, Verdicts, Calls, alternatives, then
   evidence-linked Principles, Beliefs, Values, Rules, Patterns, Outcomes, and
   CalibrationTrials.
2. **Operator/self model:** proposal-first SelfModelAssertions, Observations,
   Cues, private LexiconTerms, InterventionRules, DerivationTraces, and explicit
   ConsentGrants. Inferred readings cannot become authority without operator
   ratification; defer and confirmation remain durable review states.
3. **Chosen direction:** self-authored, operator-ratified ChosenFuture records
   are physically distinguished from inferred DefaultFuture projections.
   DirectionScores cite the exact ChosenFuture version they assess, but are
   validation-only analytical output: the canonical writer and importer never
   persist them as operator knowledge.
4. **Business/world model:** declared market and operating relationships remain
   separate from observed purchases, usage, support, results, refunds,
   retention, and referrals. Each relation states its evidence mode.

New ontology classes can be written only through the strict semantic node and
relation writer. The older derived-node path remains available solely so v3 and
legacy imports stay readable; it cannot write Level 4, Level 5, Observation,
Outcome, calibration, consent, or direction classes. This separation preserves
backward compatibility without allowing old untyped payloads into new data.

Both nodes and edges preserve evidence, provenance, authority, valid/system
time, and typed endpoint signatures. The JSON-LD graph includes payload and
evidence directly in addition to the lossless ledger, so another implementation
does not need Imprint-specific reconstruction merely to understand the graph.
