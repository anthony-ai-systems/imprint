# Limitations and Roadmap

## Current limits

- Claude Code hook payloads and lifecycle behavior can change upstream. Hook
  failures are visible and must be revalidated against new Claude Code releases.
- The default retrieval budget is byte-based and deterministic, not a promise of
  an exact model-token count.
- Canonical SQLite is single-writer and this release is not a collaborative
  database service. Data roots under recognized cloud-sync folders (Dropbox,
  OneDrive, iCloud/`CloudStorage`, Google Drive) are refused by name; other
  network or shared file systems are unsupported but not automatically detected,
  so keep the data root on a local disk.
- Cold-start input still requires conservative operator review. A source being
  parseable does not make it authoritative.
- Digest generation and profile learning are experimental, disabled by default,
  and not part of the stable acceptance claim.
- Optional graph adapters are portability conveniences. Local SQLite remains the
  authority.
- The ontology can store ZMOS/self-model, chosen-direction, business observation,
  confidence, calibration, consent, and outcome records now. Automatic ZMOS,
  Operating Portrait, Mirror Score, and intervention engines are not included.
- Scope is carried, not enforced. A record's scope row does not limit who
  receives it, so every thread still gets every eligible record. Nothing stamps a
  capture on its own yet, and a build that is rolled back to compile a stamped
  capture keeps the stamp on the event and writes no row. Any other record written
  by that build also has no row, including a successor that would have taken the
  target's row, so it reads as legacy after you upgrade again. `imprint scope set`
  can give it one.
- No software can guarantee that sensitive data is safe on a compromised host.

## Explicit non-claims

This release does not passively observe screens, reconstruct unspoken intent, or
capture decisions from Screenpipe. Screenpipe-assisted observation is roadmap
research only. Imprint does not fabricate a missing reason, infer authorship as
fact, or silently promote imported or inferred material.

## Roadmap

- Opt-in Screenpipe candidate capture with explicit consent, content boundaries,
  provenance receipts, and a complete privacy/retention threat model.
- Stable digest resolution and profile-learning lifecycles after longitudinal
  evidence and uninstall/recovery coverage exist.
- Automatic Operating Portrait synthesis, Mirror Score scenarios, drift/fossil
  alerts, and real-time sabotage intervention over the shipped typed records.
- A networked Neo4j connector layered over the shipped offline generic/Atlas
  projections, with provider-specific integration fixtures.
- Additional hook-provider adapters that preserve the same recorder/compiler
  separation.
