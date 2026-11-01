# Typed Ontology Contracts

Ontology schema `3.1.0` is additive to store and capture schema `3.0.0`. The
SQLite node/edge ledger remains canonical; these contracts prevent future
systems from assigning incompatible meanings to preserved JSON.

## Write boundary

New semantic classes must enter through:

```bash
imprint ontology add-node --input node.json --valid-from 2026-07-14T12:00:00Z
imprint ontology add-relation --input relation.json --valid-from 2026-07-14T12:00:00Z
imprint consent grant --input consent.json --valid-from 2026-07-14T12:00:00Z
imprint observation add --input observation.json --valid-from 2026-07-14T12:00:00Z
imprint outcome add --input outcome.json --valid-from 2026-07-14T12:00:00Z
```

The configured operator identity must match `operator_id`. Every inferred,
extracted, or ratified object requires canonical evidence. Unknown fields,
unknown types, invalid endpoint signatures, and authority escalation are
rejected before the ledger is mutated.

The older derived-node API exists only for v3 and legacy compatibility. It
cannot create self-model, direction, observation, outcome, consent, or
calibration classes.

## Node envelope

```json
{
  "record_schema_version": "3.1.0",
  "node_id": "urn:imprint:principle:UUID",
  "node_type": "Principle",
  "operator_id": "urn:imprint:operator:UUID",
  "payload": {"statement": "Report material source failures explicitly."},
  "provenance": {
    "status": "inferred",
    "authority_tier": "inferred_candidate",
    "actor_class": "model",
    "actor_id": "urn:imprint:model:UUID",
    "mechanism": "typed_ontology_proposal",
    "evidence_ids": ["urn:imprint:evidence:UUID"],
    "model": "provider/model-version",
    "ratifier_id": null
  }
}
```

`captured`, `extracted`, `inferred`, and `ratified` are distinct provenance
states. Ratified objects require the same operator identity as author and
ratifier. Inferred objects remain candidates and are excluded from authoritative
retrieval until explicit review.

## Relation envelope

```json
{
  "record_schema_version": "3.1.0",
  "relation_id": "urn:imprint:relation:UUID",
  "relation_type": "inferred_from",
  "source_id": "urn:imprint:principle:UUID",
  "source_type": "Principle",
  "target_id": "urn:imprint:verdict:UUID",
  "target_type": "Verdict",
  "operator_id": "urn:imprint:operator:UUID",
  "evidence_mode": "inferred",
  "why": "The proposed principle was inferred from this witnessed verdict.",
  "provenance": {}
}
```

The complete provenance object is identical to the node envelope. Both
endpoints must exist, have the declared types, and belong to the same operator.
Evidence mode is first-class and must agree with provenance.

## Semantic partitions

The judgment partition includes Case, Verdict, Call, Alternative, Principle,
Belief, Value, Rule, Pattern, Domain, Outcome, and CalibrationTrial. A Pattern
must name at least two distinct Case IDs. A missing reason remains `null` with an
explicit reason status.

The operator partition includes SelfModelAssertion, Observation, Cue,
LexiconTerm, InterventionRule, ConsentGrant, and DerivationTrace. Self-model
readings enter as inferred proposals. `review defer` records an explicit durable
deferral; ratification produces a new confirmed version without deleting the
proposal.

The direction partition includes ChosenFuture, DefaultFuture, Aim, TradeOff,
AbandonedWant, and DirectionScore. ChosenFuture is self-authored and
operator-ratified only. DefaultFuture remains inference. A DirectionScore names
the exact ChosenFuture node-version used. Storage and retrieval must not silently
blend the chosen and default partitions.

DirectionScore is a transient comparison contract, not canonical self-knowledge.
It can be validated and rendered by an analytical caller, but the canonical
writer and importer refuse to persist it.

The business/world partition includes declared customers, problems, desires,
claims, promises, expectations, mechanisms, offers, prices, channels,
objections, and proof; plus observed support, purchases, usage, results, refunds,
retention, referrals, general Observations, and Outcomes. Typed relations keep
declared theory separate from observed operating evidence.

## Retrieval partitions and authority modes

Every rendered retrieval record carries an `ontology` object with its semantic
`partition`, `type`, `path`, optional `confidence`, and plain-language
`disclosure`. There are two exceptions, both described in
`docs/configuration.md`. A compact line from the optional selector keeps
`authority`, its evidence, case, and receipt references, and a `detail` command
for reading the record locally. A short line, off by default, keeps `authority`
and `record_id`, and `imprint history <record_id>` returns the rest. Analytical
retrieval is never compact or short. The stable partitions are `judgment`, `self_model`,
`chosen_future`, `default_future`, `direction_comparison`,
`business_declared`, and `business_observed`. Declared business theory and
observed operating evidence therefore remain distinguishable even when both
are deliberately requested. `direction_comparison` labels transient analytical
output only; because DirectionScore is non-persistent, canonical stored-record
retrieval cannot populate that partition.

Retrieval defaults to `authoritative`. That mode excludes every inference and
admits a `SelfModelAssertion` only after operator ratification. `DefaultFuture`
is necessarily inferred, so it is available only through `analytical` mode,
which also requires an explicit partition request. Analytical output preserves
the `model_inference_not_operator_authority` disclosure.

`chosen_future` and `default_future` cannot be requested in the same retrieval
call. A consumer must make two explicit calls and keep their results under
their labelled partitions; the API never emits them as one unlabeled list.

## Consent

Explicit local judgment capture is the sole consent-exempt source. Conversation
imports, transcripts, Screenpipe, financial records, behavioral telemetry,
business systems, customer results, approved imports, and external connectors
are denied unless a current ConsentGrant authorizes the source class, purpose,
operation, sensitivity, retention rule, and effective interval.

Consent is checked inside the canonical writer. A JSON field that merely names
a grant is insufficient: the referenced grant must exist, belong to the same
operator, be unexpired and unrevoked, and authorize the attempted write.
Day-based retention expires from `effective_from`; it is not advisory metadata.
Create, inspect, and revoke grants through the append-only control surface:

```bash
imprint consent grant --input CONSENT_GRANT.json --valid-from RFC3339
imprint consent list
imprint consent revoke GRANT_URN --by OPERATOR_URN --reason "reason"
```

Revocation creates a new grant version and durable `consent_revoked` event.
It does not silently delete previously captured evidence; deletion remains a
separate preview-and-confirm operation so residue can be reported honestly.

## Portability

JSON-LD exports include `ontologySchemaVersion`, semantic payloads, evidence,
operator identity, provenance, authority, typed endpoints, and bitemporal
intervals directly in `@graph`, plus the complete lossless ledger. Imports
require both compatible store and ontology schema versions and verify hashes
before writing an empty store. Import does not trust those hashes for meaning:
it re-runs the typed node and relation contracts, re-checks consent for every
observed record, and enforces the authority lattice on every version — so a
document cannot smuggle ratified or model-authored authority, or a
consent-bearing observation, by pointing a record at an unexpected creation
event. Import fails closed rather than lowering any of these checks.

Two scope caveats. The lossless guarantee covers the canonical database, not the
local operator identity: `identity.json` is outside the export, so importing into
a fresh operator root mints a new operator URN and later writes against the
imported records will fail the operator-match checks unless you carry the
original identity across as well. And because import pins the exact store schema
(`3.0.0`) and column sets, a store that has taken an additive migration is not
re-importable through this path; export before migrating if you need a portable
copy. `--dry-run` validates the full document and writes nothing, not even an
empty database file.
