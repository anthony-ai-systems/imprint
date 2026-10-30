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
