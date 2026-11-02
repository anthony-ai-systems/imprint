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
- `t3_capture_origin`: `false` (default) or `true`. When `true`, Stop capture
  reads T3's local thread, project, message and provider-thread records to verify who
  wrote a T3 prompt. Prompts in threads T3 keeps in its scratch folder, its "No
  project" chats, are not credited. A record it cannot read is reported as
  `t3_record_unavailable`. See
  `docs/contracts/capture-origin.md`. Stop capture runs only where Imprint's
  hooks are registered, so a host with its own Claude config directory needs
  them registered there with `manage_hooks.py`; see Hook registration below.
  When `true`, `imprint health` also reads T3's settings file and each Claude
  home it lists, and reports homes with no hooks or another data root under
  `t3_homes`, without changing `status`. In `jev` or `shadow` mode, the prompt hook
  and `imprint retrieve --prompt` also read T3's thread and provider-thread records,
  and send Jev nothing from a scratch thread when those records place it there and
  a provider row names the session. Short of that they keep Jev and send the same
  request as any other turn: the prompt, any previous-reply excerpt and the judgment
  statements. So this does not keep prompts local. Setting `IMPRINT_SELECTOR=off`
  where the hook will see it does, and so does `selector.mode: deterministic`; see
  `docs/privacy-and-recovery.md`. Set this key after the build that
  knows it is installed. An older build refuses any config that has it, which
  fails every hook and `imprint version`, so before reinstalling an older build,
  remove `t3_capture_origin` from the config.
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
- `org.imprint.short_lines.prompt`: `false` (default) or `true`. Delivers what the
  UserPromptSubmit hook sends on short lines; see Short lines below.
- `org.imprint.short_lines.session_start`: `false` (default) or `true`. The same
  for SessionStart. Leave it off until session start can filter by client: it
  sends records from every client, and short lines let it send about three times
  as many of them.

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
  - `jev`: every non-empty UserPromptSubmit (and `imprint retrieve --prompt`)
    asks Jev which eligible judgments apply to the turn, even when no domain
    matches, and injects the picks. With `t3_capture_origin: true`, a thread T3's
    records place in its scratch folder is the exception when a provider row names
    the session, in `shadow` mode too: it gets the `deterministic` context and
    sends nothing. A nested run, a `retrieve` run T3 cannot tie to a session and
    T3 records that cannot be read all keep Jev and send the same request as any
    other turn, so this does not keep prompts local. For work that must stay
    local, set `IMPRINT_SELECTOR=off` where the hook will see it, or use
    `selector.mode: deterministic`.
  - `shadow`: calls Jev and logs the decision, but always delivers exactly the
    `deterministic` context, including after compact or resume. Use it to
    evaluate picks before switching.
- `selector.jev.endpoint`: `https://api.typesafe.ai/v1/systemone`. Must be an
  `https://` URL without embedded credentials. Redirects are refused.
- `selector.jev.model`: `jev-1.13.0`.
- `selector.jev.api_key_env`: `TYPESAFE_API_KEY`; the variable holding the key.
- `selector.jev.api_key_file`: `null`, or an absolute (or `~`) path to a file
  holding only the key. On POSIX it must be a regular file (not a symlink)
  owned by the current user with no group or other permission bits (`0600`).
  Surrounding whitespace is stripped. The environment variable wins over the
  file. The key is sent only in the `Authorization` header and is never
  logged or echoed. On Windows, Imprint checks only that the path is a regular
  file, not its owner or ACL; there, prefer the environment variable or keep
  the file in a directory only your account can read.
- `selector.jev.top`: 1–150; default 20. Most picks Jev may return.
- `selector.jev.score_floor`: 0–1; default 0.55. Minimum Jev `noul` score.
- `selector.jev.timeout_seconds`: above 0, at most 30; default 3. A total
  wall-clock deadline for one selection, including key resolution and
  parsing. On the UserPromptSubmit hook the request is also abandoned 6
  seconds after the hook command starts, whatever this value, so the
  deterministic fallback always fits inside the 10-second hook bridge limit.
- `selector.jev.prefilter_keep`: 1–10,000; default 150. A local IDF lexical
  prefilter keeps this many candidates (ties prefer newer records) before
  anything is sent; it bounds the request size.
- `selector.jev.budget_bytes`: `null` (use `context_budget_bytes`) or a value
  with the same bounds; above 32,768 requires `allow_higher_budget: true`.
  Picks are added in score order, after any reserved rules, only while the payload
  stays within it.
- `selector.jev.node_types`: the selectable judgment types; default and
  maximum `Verdict`, `Principle`, `Belief`, `Value`, `Rule`, `Pattern`.
  Business, observation, outcome, consent, self-model, chosen/default future,
  and ingested records can never be sent.
- `selector.jev.exclude_domains`: domain ids whose records are never sent. The
  prompt itself is still sent when it matches an excluded domain.
- `selector.jev.short_prompt_context`: `{"enabled": false, "below_chars": 200,
  "max_chars": 1500}`. When enabled, a prompt shorter than `below_chars`
  (1–2,000) is followed in the request by the last `max_chars` (1–4,000)
  characters of the newest assistant reply, labeled `Previous assistant
  reply:`, read from the hook's `transcript_path` with the bounded transcript
  reader. The excerpt is part of the turn's receipt scope.
- `selector.jev.stale_before`: default `null`, which is off. A UTC time like
  `2026-01-31T12:00:00Z`, which should be when this installation last took
  detector changes. A stop-hook capture from before it that no longer reads as
  feedback is left out of the request and of local delivery. Later captures met
  today's rules as they were made, and explicit captures, imports and ratified
  records never met the detector, so none of those are judged. Neither is a
  capture you have since added a reason to or reinforced, which counts as explicit.
  The record stays in the store, and session start and a deterministic fallback
  can still deliver it. Reading stops at 8,000 characters. The check gets a third
  of the time left when it starts: records it has not read by then are not sent
  that turn, and records it does not apply to still are. If that leaves nothing to
  send, the turn falls back with reason `timeout`. If everything was read and is
  stale, the turn is `no_picks` with no request and no fallback, and local records
  are still delivered.
- `selector.jev.standing_rules`: `{"enabled": false, "score_floor": 0.25,
  "rare_word_share": 0.15, "slots": 2, "keep": 30}`. A standing rule is a record
  with a sentence that reads as feedback, is not a question, and opens, after any
  `-` or `*` bullet, with "from now on", "going forward", "from here on", "always",
  "never" or "whenever", or ends with one of the first three, inside the first 400
  characters. A sentence right after a removed quote or behind a context chip is
  not recognized, because the stored text no longer shows that it opens a
  sentence. When enabled, the newest `keep` (1–150, below `prefilter_keep`) rules
  are always sent, in place of the weakest other candidates, unless `stale_before`
  leaves them out or the search for them runs out of time, which is a third of the
  time left when it starts. A rule then takes one of
  `slots` (1–5) reserved places ahead of the other picks, even below
  `score_floor`, if Jev scores it at or above this `score_floor` and its rule
  sentence shares a word with the operator's own words in the prompt, not pasted
  text or the reply excerpt, that at most `rare_word_share` of the records use, or
  that only the rule uses. A word needs three letters, and a trailing s is folded
  for that check only. Copies of one rule count once and are sent once, and a rule
  the session already has is not sent again.
  A rule without a reserved place is an ordinary candidate. Rules delivered this
  way carry `"reserved": true` on their line. This can reserve a rule on a prompt
  it does not apply to, because neither signal is exact.
- `selector.jev.log_decisions`: default `true`; see the decision log below.

To use OpenRouter's route instead, change only configuration:
`"endpoint": "https://openrouter.ai/api/alpha/decisions"`,
`"model": "typesafe/jev-1.13"`, and `"api_key_env": "OPENROUTER_API_KEY"` (or
an `api_key_file`). The request and answer shapes are the same.

Selection semantics:

- Candidates pass exactly the deterministic eligibility rules (authority,
  provenance, chosen/default future partitions, domain binding) before any
  selector filter. Domain records still require the matched domain.
- `node_types` and `exclude_domains` limit what is sent, not what is
  delivered. Records that are never sent (an excluded domain, a type outside
  `node_types`, or a non-judgment record) are injected first: the matched
  domain's for the prompt hook, every section's for `imprint retrieve`. They
  are ranked and rendered as deterministic retrieval does (no `selector`
  field); Jev's picks fill the rest of the budget.
- Records already delivered to the session, under any store snapshot, are
  excluded. The newest 256 committed receipts are scanned; unreadable ones are
  skipped. When every sendable record was already delivered, the prompt is
  `no_picks` and Jev is not called.
- Each delivered line is the ordinary JSON retrieval line plus
  `"selector": {"mode": "jev", "rank": N, "score": S}`, unless it is compact or
  short.
- Picks that all fit the budget are delivered whole. Otherwise a pick over a
  quarter of the budget is delivered as a compact line, and so is any pick the
  budget has no room left for. A compact line keeps `authority`, `case_ids`,
  `evidence_ids`, `record_id`, `source_receipt_ids` and `selector`, drops
  `case_summaries` and `ontology`, and adds `compact`, `detail` (the `imprint
  history` command for the record), `text_basis` and `text_complete`.
- A compact line's `text` is limited to 400 bytes and is always the record's own
  words. `text_basis` says which: `whole` is the record's text unchanged;
  `feedback_sentences` is every sentence that reads as feedback, in document
  order and one per line, filled backward from the last so a later correction
  survives an earlier approval; `opening` is the start of the operator's words,
  without host text or pastes. Both of the shorter forms also keep the
  message's last sentence where it fits, feedback or not, and `feedback_sentences`
  cuts one over half the limit. A reserved rule says `rule_sentences` instead: its
  rule, and nothing else. Only `whole` has `text_complete: true`.
- A compact pick that could fit the budget on its own is listed in the receipt's
  `retry_ids` and does not count as delivered, so a later turn or the
  deterministic fallback can deliver it whole. Once cut this way it is no longer
  compacted for its size, only when the budget has no room for it. A pick larger
  than the whole budget counts as delivered from its compact line and is not
  repeated.
- If Jev answers but nothing clears `score_floor` or earns a reserved place, no
  pick is injected and `selector_outcome` is `no_picks`. If Jev picked records and none could be
  delivered even compact, `selector_outcome` is `oversized_picks`, or
  `crowded_picks` when a pick would have fit without the local records. Those
  picks stay undelivered and can be chosen again on a later turn.
- A missing or unsafe key, timeout, HTTP error, malformed answer, no
  candidates, or any internal exception returns the deterministic behavior
  for that prompt, minus records Jev already delivered to the session, and
  records the reason.
- Replaying the same prompt (same text, excerpt, and domain) reuses its
  receipt instead of calling Jev again. Its file is
  `receipts/<session>/<snapshot>-domain-jev-<24 hex>.json`.
- In `jev` mode, SessionStart with `source` `compact` or `resume` forgets the
  session's delivery receipts and then delivers normally, because the host
  context was wiped. In `deterministic` and `shadow` mode the existing refresh
  behavior is unchanged. SessionStart never calls Jev.
- Hook output carries content-free `selector_method` (`deterministic`, `jev`,
  `deterministic_fallback`, or `shadow`), `selector_outcome`, and
  `fallback_reason`. The existing `selection_method` field still reports how
  the domain was matched. A turn the T3 scratch check keeps from Jev has
  `selector_outcome` `t3_scratch_thread`.

Decision log: with `log_decisions`, each selector call appends one line to
`<operator root>/logs/jev-selector.jsonl` (private file mode) with the time,
hashed session reference, receipt scope, candidates sent, picked record ids and
scores, elapsed milliseconds, echoed model, token usage, request size, outcome,
delivery outcome, stages, and fallback reason. `outcome` is Jev's own result
(`picked`, `no_picks`, or `fallback`), and it alone decides what counts as a
successful transfer. `delivery_outcome` is what the hook reported for the turn,
such as `oversized_picks`, `crowded_picks`, or `already_delivered`. `stages`
counts records `eligible`, `picked`, `delivered`, `delivered_compact`, and
`unfit`, plus `local_records` and `local_bytes` delivered ahead of the picks.
`delivery_outcome` and `stages` are null on `shadow` and `fallback` lines, which
deliver nothing of Jev's. When there is something to report, `stages` also has
`reserved`, the reserved rules delivered, `stale_withheld`, and `delivered_short`,
the picks delivered on short lines, and every line can
carry `reserved_picks`, the rules in Jev's picks, `stale_withheld`, and
`stale_scan_cut`. The log never contains prompt text, judgment text, or
the key. The line is written once the turn's delivery is settled, so a failure
after Jev answered is one `fallback` line. At 5 MiB the file rotates once to
`jev-selector.jsonl.1`.

Health reports `selector_mode`, `selector_key_present` (a boolean; the key file
is checked but not read), `selector_endpoint_host`, `selector_last_success_ts`,
`selector_fallbacks_24h`, and `network_state`: `offline` in deterministic mode,
otherwise `idle` until a request is sent and `transferred` after one. Each
sent request also updates `<operator root>/logs/jev-selector-transfer.json`
(private file mode), which holds only the last transfer and last success
timestamps. It is written even when `log_decisions` is `false`, so turning the
log off or rotating it cannot hide a transfer. `selector_fallbacks_24h` counts
decision-log lines (current and rotated file), so it stays 0 when
`log_decisions` is `false`.

## Short lines

A rule's references cost more than its words. On a whole line, 160 bytes of text
takes about 760 bytes, so a budget of 4,096 bytes carries 5 rules on a prompt and 4
at session start, which spends only its core and general sections. A short line
carries the words and leaves the rest one lookup away. This shapes delivery only:
the record, the store and the record ids in receipts are untouched, and with both
switches off delivery is unchanged.

- Each switch covers one route. `org.imprint.short_lines.prompt` covers everything
  the UserPromptSubmit hook delivers: Jev's picks, the local records ahead of them,
  the matched domain in `deterministic` mode, and the deterministic answer after a
  fallback. `shadow` delivers what `deterministic` delivers, so it goes short with
  it. `org.imprint.short_lines.session_start` covers SessionStart, which stays
  deterministic: the same store and request give the same bytes. `imprint retrieve`,
  with or without `--prompt`, and analytical retrieval always give whole lines.
- A short line has `authority`, `record_id` and `text`. `text` is always the record's
  own words, and text over 400 bytes is cut. Text that was cut adds `text_basis` and
  `"text_complete": false`, and a line without them has all of its text. Picks go
  short even when they would all fit whole.
- A captured record is cut by the compact line's rules above, which read an operator's
  message and drop what it quotes or pastes. A short line also carries documents and
  imports, where a quote can be the rule itself, so any other record is cut as it
  stands: its start and its last sentence, with `text_basis` `stored_text`.
- A reserved rule keeps the record's last sentence after its rule, and `text_basis`
  stays `rule_sentences`. A later sentence can overrule the rule, and the reserved
  mark would otherwise vouch for what it overruled. The last sentence takes at most
  half of the 400 bytes, so the rule always has the rest. A compact line says the rule
  alone.
- The first short line of a payload also has `"detail": "imprint history
  <record_id>"`, which holds for every line in it.
- `selector` appears only on a reserved rule, as `{"reserved": true}`. Nothing reads
  the rest of the block: the order of the lines is the rank, and the decision log
  has each pick's score.
- `imprint history <record_id>` returns what a short line leaves out. Its
  `whole_line` is the record's whole retrieval line, with `case_ids`,
  `case_summaries`, `evidence_ids`, `ontology` and `source_receipt_ids`. It is `null`
  for a node retrieval never delivers, such as Evidence, and for a record closed
  since, whose `versions` and `dispositions` say why.
- A rule whose whole text went out on a short line counts as delivered, so it is
  not sent again. One whose text was cut is listed in `retry_ids`, and when it is
  picked again it arrives on a whole line, as long as there is room, and is then
  done. Session start and the fallback list every record they cut, because Jev's
  budget can be larger than theirs. A pick whose whole line can never fit Jev's
  budget counts as delivered from its short line, which can be its second time out
  cut. So does a local record, cut or not, because nothing picks it and it would
  otherwise go out cut on every turn.
- Only a pick can send a cut record whole, so nothing else sends one again. The
  deterministic fallback keeps out records still owed a whole line as well as those
  delivered, and a turn Jev owns keeps them out of its local records. A cut record
  therefore goes out once in an outage, however often the store moves, and its
  place goes to a record not yet sent. On whole lines the fallback can still deliver
  a compact pick, because there it has the whole line to send.
- Once a section has refused a line, the rest of its records over 400 bytes are
  passed over without being cut, because cutting reads all of a long record and
  session start meets every eligible one. Shorter records can still fill the room.

With a 56-byte record id, 160 bytes of text and a budget of 4,096 bytes, 15 rules
fit a prompt and 11 fit session start. Session start reaches 12 only with text of
146 bytes or less: 12 lines and the lookup have 252 bytes a line, and the line's
keys and id take 106 of them. A prompt of nothing but `imported_floor` rules holds 14,
and so does one with 5 reserved rules. Text cut to 400 bytes costs more than these.

Both keys are dotted, so an older build keeps them and reads nothing into them, and
rolling back needs no change to the config.

## Environment overrides

- `IMPRINT_CONFIG`: config path.
- `IMPRINT_SELECTOR`: any non-empty value other than `jev` or `shadow` (for
  example `off`, `deterministic`, `0`, `false`, or `disabled`) forces
  deterministic retrieval over any config. `jev` and `shadow` leave the
  configured mode in force. It can only turn the selector off; no value turns
  it on. It is read from the process that sends, so set it where the hook will
  see it: under T3, the instance's `environment`, the home's `env` or the server's
  environment. A shell export reaches only what that shell runs.
- `IMPRINT_DATA_ROOT`: default data root used when config omits `data_root`.
- `IMPRINT_INSTALL_ROOT`: installer/application destination.
- `CLAUDE_SETTINGS_PATH`: the Claude Code settings path an installer registers
  hooks in as part of a full install. To add hooks to one more home, use
  `manage_hooks.py`; see Hook registration.
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

To add or remove hooks in one Claude home, such as a host's own config directory,
run the packaged `manage_hooks.py` from the install root, under `umask 077`. It
edits only that home's settings file, in the same way and with the same backup.
Do not use the installer for this, whatever its `--settings` option suggests: it
is a full reinstall. It sets `operator_slug` back to `default` unless `--operator`
is passed, and `data_root` back to its default unless `--data-root` is.

```bash
umask 077
INSTALL_ROOT="${IMPRINT_INSTALL_ROOT:-$HOME/.local/lib/imprint-local}"
PYTHON="$INSTALL_ROOT/venv/bin/python"
SETTINGS="/path/to/claude-home/settings.json"

# Add the hooks to this home.
"$PYTHON" "$INSTALL_ROOT/tools/manage_hooks.py" register \
  --settings "$SETTINGS" --python "$PYTHON" --hooks-dir "$INSTALL_ROOT/hooks"

# Check them. Exit 0 means each hook is registered once.
"$PYTHON" "$INSTALL_ROOT/tools/manage_hooks.py" status \
  --settings "$SETTINGS" --python "$PYTHON" --hooks-dir "$INSTALL_ROOT/hooks"

# Remove only Imprint's hooks from this home.
"$PYTHON" "$INSTALL_ROOT/tools/manage_hooks.py" unregister \
  --settings "$SETTINGS" --python "$PYTHON" --hooks-dir "$INSTALL_ROOT/hooks"
```

`register` and `unregister` replace the settings file, so `umask 077` keeps the new
one private. On Windows the same commands use `venv\Scripts\python.exe` and
`tools\manage_hooks.py` under the install root, which defaults to
`%LOCALAPPDATA%\ImprintApp\app`, and `umask` does not apply.
