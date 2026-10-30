# Configuration

The default config is `~/.config/imprint/config.json` on macOS/Linux and
`%APPDATA%\Imprint\config.json` on Windows. Set `IMPRINT_CONFIG` to use a
different file. The installer merges its required values into an existing JSON
object and preserves unknown namespaced extensions.

## Fields

- `config_version`: configuration contract version; `3.0.0` for this release.
- `data_root`: absolute, local directory above all operator directories.
- `operator_slug`: lowercase letters, digits, and hyphens; default `default`.
- `node_id`: this machine's spool identity; default `primary`.
- `compiler`: only one node may be `true` for a shared logical Imprint.
- `context_budget_bytes`: 4,096–131,072. The tested/default value is 32,768.
  Values above 32,768 require `allow_higher_budget: true`.
- `spool_retention_days`: 1–36,500; default 30. Pruning is never automatic and
  deletes only this configured `node_id`'s hash-verified, acknowledged inputs.
- `domains`: optional closed array of domain packs. Each pack declares a safe
  `domain_id`, `public_label`, optional `safe_paths`, optional `keywords`, and
  `frozen`. Selection order is explicit ID, longest safe path, then keyword.
  Ties inject no domain; domain retrieval is delivered once per session and
  snapshot and never repeats the core/general session-start payload.
- `experimental.digest`: experimental; disabled by default.
- `experimental.profile_learning`: experimental; disabled by default.
- `selector`: optional prompt-time relevance selection; see below. The default
  `mode` is `deterministic`, which makes no network request.

The release artifact includes `config.example.json`. Replace its placeholder
absolute path before using it.

## Optional relevance selector

`selector` is a closed object; unknown keys and wrongly typed values (including
booleans where numbers are expected) are rejected. A partial object overlays
the defaults, so `{"selector": {"mode": "jev"}}` is a complete opt-in.

- `selector.mode`: `deterministic` (default), `jev`, or `shadow`.
  - `deterministic`: today's behavior. SessionStart delivers core and general
    context; UserPromptSubmit injects the matched domain once per session and
    snapshot, or skips when no domain matches.
  - `jev`: every non-empty UserPromptSubmit asks Jev which eligible judgments
    apply to the turn, even when no domain matches, and injects the picks.
  - `shadow`: calls Jev and logs the decision, but always delivers exactly the
    `deterministic` context. Use it to evaluate picks before switching.
- `selector.jev.endpoint`: `https://api.typesafe.ai/v1/systemone`. Must be an
  `https://` URL without embedded credentials. Redirects are refused.
- `selector.jev.model`: `jev-1.13.0`.
- `selector.jev.api_key_env`: `TYPESAFE_API_KEY`; the variable holding the key.
- `selector.jev.api_key_file`: `null`, or an absolute (or `~`) path to a file
  holding only the key. On POSIX it must be a regular file (not a symlink)
  owned by the current user with no group or other permission bits (`0600`).
  Surrounding whitespace is stripped. The environment variable wins over the
  file. The key is sent only in the `Authorization` header and is never
  logged or echoed.
- `selector.jev.top`: 1–150; default 20. Most picks Jev may return.
- `selector.jev.score_floor`: 0–1; default 0.55. Minimum Jev `noul` score.
- `selector.jev.timeout_seconds`: above 0, at most 30; default 3. A total
  wall-clock deadline for one selection, including key resolution and
  parsing. Keep it well under the 10-second hook bridge limit.
- `selector.jev.prefilter_keep`: 1–10,000; default 150. A local IDF lexical
  prefilter keeps this many candidates (ties prefer newer records) before
  anything is sent; it bounds the request size.
- `selector.jev.budget_bytes`: `null` (use `context_budget_bytes`) or a value
  with the same bounds; above 32,768 requires `allow_higher_budget: true`.
  Picks are added in score order only while the payload stays within it.
- `selector.jev.node_types`: the selectable judgment types; default and
  maximum `Verdict`, `Principle`, `Belief`, `Value`, `Rule`, `Pattern`.
  Business, observation, outcome, consent, self-model, chosen/default future,
  and ingested records can never be sent.
- `selector.jev.exclude_domains`: domain ids whose records are never sent.
- `selector.jev.short_prompt_context`: `{"enabled": false, "below_chars": 200,
  "max_chars": 1500}`. When enabled, a prompt shorter than `below_chars`
  (1–2,000) is followed in the request by the last `max_chars` (1–4,000)
  characters of the newest assistant reply, labeled `Previous assistant
  reply:`, read from the hook's `transcript_path` with the bounded transcript
  reader. The excerpt is part of the turn's receipt scope.
- `selector.jev.log_decisions`: default `true`; see the decision log below.

To use OpenRouter's route instead, change only configuration:
`"endpoint": "https://openrouter.ai/api/alpha/decisions"`,
`"model": "typesafe/jev-1.13"`, and `"api_key_env": "OPENROUTER_API_KEY"` (or
an `api_key_file`). The request and answer shapes are the same.

Selection semantics:

- Candidates pass exactly the deterministic eligibility rules (authority,
  provenance, chosen/default future partitions, domain binding) before any
  selector filter. Domain records still require the matched domain.
- Records already delivered to the session, under any store snapshot, are
  excluded. The newest 256 committed receipts are scanned; unreadable ones are
  skipped.
- Each delivered line is the ordinary JSON retrieval line plus
  `"selector": {"mode": "jev", "rank": N, "score": S}`.
- If Jev answers but nothing clears `score_floor`, nothing is injected.
- A missing or unsafe key, timeout, HTTP error, malformed answer, no
  candidates, or any internal exception returns exactly the deterministic
  behavior for that prompt and records the reason.
- Replaying the same prompt (same text, excerpt, and domain) reuses its
  receipt instead of calling Jev again. Its file is
  `receipts/<session>/<snapshot>-domain-jev-<24 hex>.json`.
- In `jev` and `shadow` mode, SessionStart with `source` `compact` or `resume`
  forgets the session's delivery receipts and then delivers normally, because
  the host context was wiped. In `deterministic` mode the existing refresh
  behavior is unchanged. SessionStart never calls Jev.
- Hook output carries content-free `selector_method` (`deterministic`, `jev`,
  `deterministic_fallback`, or `shadow`), `selector_outcome`, and
  `fallback_reason`. The existing `selection_method` field still reports how
  the domain was matched.

Decision log: with `log_decisions`, each selector call appends one line to
`<operator root>/logs/jev-selector.jsonl` (private file mode) with the time,
hashed session reference, receipt scope, candidates sent, picked record ids and
scores, elapsed milliseconds, echoed model, token usage, outcome, and fallback
reason. It never contains prompt text, judgment text, or the key. At 5 MiB the
file rotates once to `jev-selector.jsonl.1`.

Health reports `selector_mode`, `selector_key_present` (a boolean; the key file
is checked but not read), `selector_endpoint_host`, `selector_last_success_ts`,
`selector_fallbacks_24h`, and `network_state`: `offline` in deterministic mode,
otherwise `idle` until the decision log records a sent request and
`transferred` after one.

## Environment overrides

- `IMPRINT_CONFIG`: config path.
- `IMPRINT_SELECTOR`: `off` or `deterministic` forces deterministic retrieval
  over any config. It can only turn the selector off; no value turns it on.
- `IMPRINT_DATA_ROOT`: default data root used when config omits `data_root`.
- `IMPRINT_INSTALL_ROOT`: installer/application destination.
- `CLAUDE_SETTINGS_PATH`: Claude Code settings path used by installers.
- `XDG_CONFIG_HOME` and `XDG_DATA_HOME`: respected on POSIX systems.

## Multi-operator and multi-node use

Each operator gets a separate directory and canonical database. Do not point two
operators at the same slug. Multiple machines may emit immutable per-node spool
files, but exactly one configured compiler may consume them. Do not place the
canonical SQLite database in Dropbox, OneDrive, Google Drive, iCloud Drive, an
NFS share, or another synchronized/shared-writer location.

## Hook registration

The installer edits only `hooks.SessionStart`, `hooks.UserPromptSubmit`, and
`hooks.Stop`. Before every edit it creates a timestamped settings backup. Managed
entries contain `imprint-local-managed-hook`; reinstall first removes those
entries, then adds one copy of each intended hook. Unrelated entries are retained.
