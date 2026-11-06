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

The `record_scope` table holds at most one row per retrievable record: whose it
is (`client`, `central`, `system`, `unresolved` or `held`), an optional client
and project, a delivery tag and rank, and any `overrides`. The row is keyed by
node, so a new version keeps it. A record with no row is legacy. A client or
project id has one shape: lower case letters and digits joined by single hyphens,
such as `north-wind`. Python refuses any other by name, and so does the table,
which also refuses `overrides` that are not a JSON list. The delivery tag and rank
are checked in Python only, so their rules can still change. This release
only carries scope: no delivery route reads a row, and with no row and no scope
extension the output matches the previous release byte for byte. The store and
record schema versions do not move, and an older build still opens the store. The
snapshot id moves at the first row, and then whenever a row changes.

- **Capture.** An envelope may carry the `org.imprint.scope` extension, as
  `{schema_version, payload}`. The compiler writes the row in the capture's own
  transaction and quarantines a spool whose stamp is invalid. A stamp cannot say
  `central` and cannot carry `overrides`. Nothing adds the extension yet, so the
  Stop hook, `imprint capture` and a `CapturePipeline.capture` caller that passes
  no `extensions` all write legacy records.
- **A `held` stamp** can list `candidate_client_ids`. The list stays on the event,
  where `imprint history` shows it in `event_payload`, and has no column.
- **`imprint scope set`** appends a `scope_changed` event and updates the row in
  one transaction, moving only what it names, and `resolved_by` becomes `explicit`.
  It is the only way out of `held`. It takes `--override` only for a current central
  record, and `scope_changed` is the only event that carries overrides. Because a
  row sits beside the node's versions, a change adds no version. `imprint history`
  lists the event and shows the current row as `scope`, or no `scope` when there is
  no row.
- **Other paths.** `ontology add-node`, `observation add` and `outcome add` take
  `--scope-kind`, `--scope-client` and `--scope-project`, and write no row without
  them. `imprint ratify apply` stamps a new node `unresolved`, except a Domain.
  `import_legacy_principles` stamps `unresolved` unless given a scope.
  `imprint ingest keep` writes no row yet.
- **Supersession.** A successor keeps its own row. With none, it takes a copy of
  the target's row in the same event. Superseding a central record needs a
  successor that has its own row and is not `client`, because a client record adds
  to a central one through `overrides` and never replaces it.
- **Who writes `central`.** Nothing in this release does. `imprint scope set`, a
  capture stamp and the ontology flags all refuse it, and import accepts it only on
  a ratified record.
- **Retrieval.** `RetrievalRecord.scope` carries the row. Eligibility, ordering and
  rendering ignore it.

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
