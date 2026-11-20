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
  `t3_record_unavailable`, and a person's prompt refused only because its entrypoint
  is not one T3 has shipped (`sdk-ts` or `t3-code`) as `t3_entrypoint_unknown`. See
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
  for SessionStart. Leave it off until session start can filter by client and by
  age: it sends records from every client, it does not read
  `selector.jev.stale_before`, and short lines let it send about three times as
  many of both.
- `org.imprint.client_carrier.queue`: `null` (default) or an absolute path. The
  file the client-work carrier step appends to; see Client-work carrier queue
  below. Unset, the step does nothing.
- `org.imprint.client_carrier.clients`: `null` (default) or an absolute path. The
  client list the carrier step reads at run time. Unset or unreadable, it names no
  client and writes nothing.
- `org.imprint.selector.failure_notice`: `false` (default) or `true`. When `true`, a
  prompt turn whose Jev selection failed and that delivered nothing else carries one
  line saying so; see Failure notice below.
- `org.imprint.decide.purposes`: `{}` (default). Lets named purposes ask Jev typed
  questions outside the selector; see Typed decisions below. Empty, nothing is sent.
- `org.imprint.health.capture_silence_days`: `null` (default) or a whole number of
  days, 1 to 365. When set, `imprint health` degrades with `capture_silent` when no
  capture has landed in that many days while the Stop hook ran at least 10 times in
  them on turns that could have captured; see Capture silence below. Unset, health never asks, and adds none of the
  `capture_silence_` fields.

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
`reserved`, the reserved rules delivered, `stale_withheld`, `delivered_short`, the
picks delivered on short lines, `local_unfit`, the local records that did not fit,
and `session_held`, the records this session already had on a turn with nothing left
to send. Every line can
carry `reserved_picks`, the rules in Jev's picks, `stale_withheld`, and
`stale_scan_cut`. The log never contains prompt text, judgment text, or
the key. The line is written once the turn's delivery is settled, so a failure
after Jev answered is one `fallback` line. At 5 MiB the file rotates once to
`jev-selector.jsonl.1`.

Health reports `selector_mode`, `selector_key_present` (a boolean; the key file
is checked but not read), `selector_endpoint_host`, `selector_last_success_ts`,
`selector_fallbacks_24h`, and `network_state`: `offline` in deterministic mode,
otherwise `idle` until a request is sent and `transferred` after one. A typed-decision
purpose that is enabled also keeps it from `offline`; see Typed decisions below. Each
sent request also updates `<operator root>/logs/jev-selector-transfer.json`
(private file mode), which holds only the last transfer and last success
timestamps. It is written even when `log_decisions` is `false`, so turning the
log off or rotating it cannot hide a transfer. `selector_fallbacks_24h` counts
decision-log lines (current and rotated file), so it stays 0 when
`log_decisions` is `false`. It counts every fallback, including a turn with
nothing to send, so it cannot tell a failed selection from an empty one. The hook
route log below can.

## Hook route log

Each turn of the two hooks that deliver memory, session start and the prompt hook,
appends one line to `<operator root>/logs/hook-routes.jsonl` (private file mode),
and so does each Stop; see Stop lines below. This happens on every selector mode,
whatever `log_decisions` says, and it never changes what a hook delivers or prints.
At 5 MiB the file rotates once to `hook-routes.jsonl.1`. The health hook writes no
line, and `imprint retrieve` is not a hook, so it writes none either.

Each turn of a memory hook lands on exactly one `outcome`:

- `delivered`: bytes went out, and nothing below applies.
- `nothing_relevant`: the turn had nothing to deliver. No matched domain, no picks
  from Jev, an empty store, or a turn with nothing to send to Jev (`no_candidates`)
  all end here, and so does a repeat of such a turn. Empty is not dead.
- `already_delivered`: this session was already given it. That is a repeat of a turn
  that delivered, or a turn with nothing left to send because the session has it.
  It goes by the session's receipts, so on `deterministic` and `shadow` a domain prompt
  after a compact reads this too: only `jev` forgets its receipts when the context is
  wiped.
- `scratch_skip`: the T3 scratch check kept the turn from Jev. T3 is asked only on a
  turn that would call Jev, so a `deterministic` store never reaches this.
- `failed`: selection failed, the hook did not finish, or the host never got the
  output. Records that cannot fit in the budget, with nothing else delivered, are
  also `failed`, because they are not the same as nothing relevant. Jev's picks read
  `oversized_picks` or `crowded_picks`, and any other records `oversized_records`. A
  repeat of such a turn stays `failed`.

A failed selection stays `failed`, and a scratch turn stays `scratch_skip`, even
when the deterministic path then delivers its domain, so a dead Jev on a matched
domain does not read as `delivered`. `bytes` says what went out either way.

A line holds `ts`, `route` (`session_start`, `user_prompt_submit` or `stop_capture`),
`outcome`, `detail`, `bytes`, `mode` (the selector mode in force), `method` and `answered`.
`method` is the hook output's `selector_method`, `deterministic` on a turn that
reports none, which includes every session start, or null when the hook could not say
how the turn went. `answered` is true only when Jev replied on that turn, and it stays
true if a local step then failed. `detail` is a closed code: the fallback reason, the
selector outcome, the domain diagnostic, the hook bridge's error, or
`oversized_records`. A line never holds a prompt, a statement, a record id, a domain
or a session.

A turn that carried the failure notice (see Failure notice below) adds `"notice": true`,
and its `bytes` count the notice. No other line has the key.

Health reports the last day as `hook_route_outcomes_24h`, per route and outcome.
`hook_route_evidence` reads `unavailable` when a `jev` store has
`selector_failure_limit` or more fallbacks in the decision log and no route line in
the window. The log took no lines, so its counts and a run of 0 say nothing. Two
degraded reasons come from this and apply only under `selector.mode: jev`. `shadow`
and `deterministic` stores, and a store `IMPRINT_SELECTOR` turns off, never raise
them.

- `selector_key_unavailable`: at once, with no traffic needed, when the key check
  behind `selector_key_present` fails. The check reads metadata only and never the
  key. It fails when the variable is unset and there is no key file, or when the file
  is empty, over 4096 bytes, not a private regular file you own, or has no owner read
  bit. A key only reading it could judge, such as a malformed value or a file of
  blanks, passes this check and surfaces through `selector_failures_repeated`. The
  check sees the environment of the process that runs it, so a key set only for the
  hooks reads as missing from another shell.
- `selector_failures_repeated`: 3 or more failed selections in a row
  (`selector_failure_limit`), all from the last 24 hours. `selector_failures_in_a_row`
  is the count. A failed selection is a prompt turn under `jev` that fell back for a
  reason other than `no_candidates`. That includes one that failed before it could
  ask, such as a key selection refused or a local time budget already spent, so
  `detail` says which it was. One timeout does not degrade health, and neither do two.
  A turn that used Jev's answer ends the run and clears the reason at once, even if its
  picks would not fit. A failure after Jev replied adds to the run, because the agent
  got no picks, and `answered` true on the line tells it apart. Any other turn that never asked Jev, such
  as a replay, a scratch turn, session start or a turn with nothing to send, neither
  adds to a run nor ends it, and neither does `oversized_records`. A failure older
  than 24 hours no longer counts, so old history ages out by itself. A hook that timed
  out, crashed, gave output it could not use, reported no route line or could not
  start is not a failed selection. Each of those leaves a file in `logs/hook-failures`,
  which `hook_failures_present` reports. Damaged log lines count as nothing. Only an
  unbroken run raises this reason, so a Jev that fails on and off shows only in
  `selector_fallbacks_24h`, `hook_route_outcomes_24h` and `selector_failure_share`.

Under `selector.mode: jev` only, health also reports `selector_failure_share`: of the
newest `window` (50) selections in the last 24 hours, how many there were (`sample`),
how many failed (`failed`), and `share`, their ratio to four places. A selection is a
turn that used Jev's answer or a failed selection as defined just above, so a turn
with nothing to send, a replay, a scratch turn and `oversized_records` are not in it.
`share` is `null` until `sample` reaches `min_sample` (10). It is reported only: it
never raises a degraded reason, and what degrades health is still the run alone.
`shadow` and `deterministic` stores do not have the field.

## Failure notice

With the selector on `jev`, a prompt turn whose selection fails falls back to the
deterministic answer. With no matched domain that is empty context, so the agent
cannot tell nothing relevant from memory being down. With
`org.imprint.selector.failure_notice: true`, such a turn carries this one line
instead:

```text
Imprint: judgment retrieval was unavailable this turn (reason: timeout).
```

The reason is the turn's `fallback_reason`, one of `missing_key`, `key_file_unsafe`,
`timeout`, `http_error`, `bad_response` or `exception`. The line holds nothing else:
no prompt, statement, record or path. The hook output also gains
`"selector_notice": "selection_failed"`, and the route line `"notice": true`.

- The line goes out only when nothing else does. A matched domain whose records the
  fallback still delivers carries those records and no line. A domain the session
  already has, so the fallback delivers nothing, carries the line.
- `no_candidates` is an empty pool, not a failure, and gets no line. Neither does a
  turn Jev answered with no picks, a replay, a T3 scratch turn, or any turn under
  `shadow` or `deterministic`, which deliver the deterministic answer by design.
- A hook that timed out or crashed before it could print is outside this: the bridge
  emits empty context, as before, and leaves a file in `logs/hook-failures`.
- With the key `false` or absent, every byte a hook emits and every route line is
  unchanged. The key is dotted, so an older build keeps it and reads nothing into it.

## Typed decisions

`imprint.decide` is a small package for asking Jev more than relevance. Nothing in it
is switched on, the live selector does not use it, and no hook imports it; a test holds
both. It has four parts.

- Question builders for the three documented primitives. `noul(instructions)` asks a
  yes or no question and its answer is the probability of yes. `choice(instructions,
  {name: description, ...})` asks for one of 1 to 255 named options, in the order given.
  `score(instructions, [level, ...])` asks for a place on 1 to 10 ordered levels, the
  first being level 0. Instructions are 1 to 4,000 characters and each description or
  level 1 to 1,000. Question and option names are lowercase identifiers
  (`[a-z][a-z0-9_]{0,63}`) chosen in code, so the log can carry them. The Noul shapes are
  the selector's, confirmed against the live API. The Choice and Score shapes come from
  TypeSafe's public documents only and are unverified against the live API, so answers
  are read defensively: fields the answer does not need are ignored, a missing or out of
  range `choice` or `score`, or a `confidence` or probability that is present and not a
  number from 0 to 1, rejects that answer, a Choice naming an option the request did not
  send is rejected, and `probabilities` is read as an object or as an array in request
  order. Either way it is kept as sent, keyed by option name or by level number (`"0"`,
  `"1"`, ... written plainly), and a level or option the reply leaves out stays out:
  nothing is filled in. An answer that cannot be read is dropped on its own and never
  costs the others in the reply.
- One call, `ask(purpose, state, questions, config=..., root=...)`. `state` is text or a
  JSON object, at most 16,000 characters. It sends at most 1,000 questions and 1 MiB in
  one `POST` to `selector.jev.endpoint` with `selector.jev.model` (or a model the caller
  pins) and the same key, under the selector's transport rules: no redirects, a bounded
  response, and one deadline (`selector.jev.timeout_seconds`, or the caller's, at most
  30 seconds) over key resolution, the request and parsing. It never raises and never
  retries. Its `outcome` is `answered`, `disabled`, `capped` or `fallback`, and its
  `reason` is one of `unknown_purpose`, `switched_off`, `purpose_off`, `cap_reached`,
  `cap_unavailable`, `invalid_request`, `missing_key`, `key_file_unsafe`, `timeout`,
  `http_error`, `bad_response` or `exception`. A reply with some valid answers is
  `answered`, with the rest named in `dropped`. A reply with none is `bad_response`.
- A purpose registry. Every call names one of `selector`, `capture_classify`,
  `event_match`, `completion_check`, `draft_check`, `action_guard` and `probe`. Each has
  a mode: `off` (the default), `shadow` or `enforce`. Under `shadow` the result carries
  the answers and says it is shadow, and the caller must not act on them. The module only
  reports the mode; each caller decides what it means. `selector` is reserved for a later
  change to the selector, which does not use it today.
- A daily cap per purpose, kept locally in `logs/decide-calls.json` under the data root
  by UTC day. A call takes one from the cap just before it sends, after the request is
  built, the key is found and the deadline is checked, so a refused request, a missing
  key or a call already out of time costs nothing. A call whose deadline passes in the
  moment between taking its count and sending is still counted, so the cap counts calls
  taken, never fewer than calls sent. A counter that is not a small regular file of the
  expected shape fails closed with `cap_unavailable`: delete it to start the day's count
  again. Calls from separate processes take turns through `logs/decide-calls.lock`
  (`flock` on POSIX, a byte lock on Windows); a call never blocks on it, and one that
  cannot take the lock before its deadline, or that runs where no file lock exists, is
  `cap_unavailable` and sends nothing.

`org.imprint.decide.purposes` maps a purpose to `{"mode": ..., "daily_cap": ...}`:

```json
{"org.imprint.decide.purposes": {"probe": {"mode": "shadow", "daily_cap": 24}}}
```

- `mode`: `off` (default when left out), `shadow` or `enforce`.
- `daily_cap`: 0 to 100,000; 0 (default when left out) sends nothing.

A purpose not named is `off`. The config never fails to load over this key, since every
hook loads it: an entry this build cannot read is `off` instead. That covers a purpose
it does not know (a later build's, or a typo), an entry with any key other than `mode`
and `daily_cap`, and a mode or cap outside the values above; a value that is not an
object turns every purpose off. The purposes are a closed list in code, so no spelling
can open one. `imprint health` counts such entries as `decide_purposes.ignored`.
`IMPRINT_DECIDE` set to any non-empty value turns every purpose off, and so does an
`IMPRINT_SELECTOR` value that turns the selector off. No environment value turns one on.
The key is dotted, so an older build keeps it and reads nothing into it.

Health reports egress truthfully once a purpose can send. With any purpose enabled (a
mode other than `off` and a cap above 0), `network_state` is never `offline`, even under
a `deterministic` selector: it is `idle`, or `transferred` once a typed decision has sent
bytes, which `ask` stamps in `logs/decide-transfer.json` (one timestamp, nothing else),
and `last_transfer_age_seconds` is the newer of the selector's and the purposes'
transfers. `metrics.decide_purposes` gives `enabled` and `ignored` counts. With no
purpose named, health is exactly as before and `decide_purposes` is absent. Health
reads the config only, so an environment switch that turns the purposes off does not
change what it reports.

Each call that is not `disabled` appends one line to `logs/decide-<purpose>.jsonl`
(private file mode, rotated once at 5 MiB): `ts`, `purpose`, `mode`, `outcome`,
`reason`, `questions` (name to type), `answers` (name to type and values: `noul`;
`choice`, `confidence` and `probabilities`; or `score`, `confidence` and
`probabilities`), `dropped`, `elapsed_ms`, `usage`, `model_echoed`, `request_bytes` and
`request_sent`, plus `dropped_shapes`, the field names and value kinds of answers that
could not be read. It never holds the state, an instruction, a description or the key.

### Offline selector evaluation

`imprint selector-eval --cases CASES --scores SCORES` measures today's selection path
on labelled cases with no network, config or store. It runs the real lexical
prefilter, today's pick policy on the saved scores (at or above the floor, best first,
at most `top`), and the renderer the prompt hook uses for Jev's picks, then reports
`recall_at_shortlist` (gold candidates the shortlist kept), `precision_rendered` (gold
among what would be rendered within the budget), `unrelated_pickups` (rendered
candidates that are not gold) and per-case counts. Options: `--shortlist` (default
150), `--budget` (32,768), `--score-floor` (0.55), `--top` (20) and `--short-lines`.
Standing rules and the stale check are not modelled. Records are built from the
fixture with synthetic ids, so line sizes are close to a real store's, not equal.
Output holds ids and counts only. `tests/fixtures/selector_eval/` has a small synthetic
pair; the module docstring in `src/imprint/decide/evaluate.py` gives both formats.

### Jev probe

`tools/jev_probe/run_probe.py` runs a fixed 24-call synthetic plan through `ask` as
purpose `probe`, pinned to `jev-1.13.0`. Calls 1 to 12 send two invented states (about
100 and 1,000 tokens) with 1, 10 and 150 Nouls, twice, interleaved, to show how cost and
latency grow with state length and question count. Its Noul statements are shorter
than live ones, so absolute token counts run lower than the selector's. Calls 13 to 24
send twelve invented labelled cases (four applicable, four unrelated, four conflicting
or with too little to go on), each as one Noul, one Choice (`required_here`,
`conflicts_with_higher_instruction`, `irrelevant`, `not_enough_evidence`) and one Score
(no effect, useful background, changes the next action, prevents a consequential error)
in the same request.

Run with no flags, it prints the plan and each request's size and sends nothing. It
sends only with `--send-approved-calls 24`, a `--config` whose purposes enable `probe`
with a cap that leaves 24 calls today, no environment switch turning it off, and a key
present; otherwise it prints why and sends nothing. It stops at the first call that is
not answered and never retries. Its output and the purpose log hold counts, sizes,
usage, latency, answer shapes and values, and case ids.

## Stop lines

A Stop that captures nothing looks the same from outside whether nobody gave feedback
or every prompt was refused. So each Stop appends one `stop_capture` line to the same
file, under the same rotation, and lands on exactly one `outcome`:

- `captured`: the capture's envelope was written. `detail` is `compiled` when it was
  compiled in the same call and `spool_only` when it was not: the compiler is off, or
  the compile or its acknowledgement failed after the write. That failure still exits
  as one, blocks the stop once under the bridge and leaves a file in
  `logs/hook-failures`, but the turn is captured and its envelope stays in the spool.
- `not_feedback`: the detector did not read the operator's words as feedback, or
  nothing of the operator's was left once pastes and quotes were removed.
- `origin_unverified`: the newest prompt came by a transport that does not say who
  wrote it, and nothing vouched for it. This is the receipt's `unverified_provenance`.
- Each other reason the Stop receipt can give, under its own name: `machine_origin`,
  `synthetic_transcript_entry`, `no_operator_message`, `operator_turn_expired`,
  `operator_turn_already_captured`, `feedback_text_unavailable`,
  `operator_text_oversized`, `transcript_unavailable`, `transcript_refused`,
  `transcript_malformed`, `transcript_tail_exhausted`, `t3_record_unavailable`,
  `t3_session_ref_unreadable`, `t3_session_unmatched`, `t3_prompt_unmatched`,
  `t3_marked_window`, `t3_scratch_thread` and `t3_entrypoint_unknown`.
  `docs/contracts/capture-origin.md` says what each means.
- `failed`: the capture raised before its envelope was written, or the hook timed
  out, crashed, gave output it could not use, reported no line or could not start, as
  on the other routes. A hook the bridge timed out reads `failed` even if it had
  written its envelope, because the bridge cannot see how far it got. `detail` is
  the error code: `hook_exception` when the hook ran on its own, otherwise the
  bridge's, such as `hook_action_failed`. Input a retry cannot change is `failed`
  with `hook_input_invalid`, as it is on the other routes, and so is any receipt
  reason not listed above, with `unknown_reason`, so one added later cannot pass as
  a skip.

For a skip, `detail` is the receipt's `provenance_basis` where it gives one, such as
`unverified`, `structure`, `origin` or `transport`, and otherwise null. A Stop line
always has `bytes` 0, `method` null and `answered` false. Its `mode` is the selector
mode in force, except on a Stop that `IMPRINT_CAPTURE_ORIGIN` marks, which is skipped
before the config is read: under the hook bridge its line has `mode` null, and run
on its own, outside the bridge, it writes no line at all. A Stop line holds no prompt,
no statement, no session and no path. What the Stop prints is unchanged.

`hook_route_outcomes_24h.stop_capture` counts the last day of these lines by outcome,
beside the other routes. `capture_newest_age_seconds` is the age of the newest
capture envelope in the spool, or -1 when there is none. Every spool file is a
capture, from the Stop hook or `imprint capture`, on this node or another whose
spool this store holds, so this is the age of the newest capture that landed,
whether or not it has compiled. `imprint spool prune` removes old acknowledged
envelopes, so after a prune it can read older or -1.

## Capture silence

With `org.imprint.health.capture_silence_days` set to a number of days, `imprint
health` adds the degraded reason `capture_silent` when both hold:

- no capture has landed in that many days: no `captured` Stop line is in the
  window, and `capture_newest_age_seconds` is -1 or older than the window, and
- the Stop hook wrote at least 10 lines in the same window
  (`capture_silence_min_stops`) on turns that could have captured: any outcome but
  `machine_origin` and `t3_scratch_thread`. Those turns are automation, or a T3
  scratch thread where no prompt is credited, so they could never capture.

A store nobody used in the window is quiet, not silent, so it is not degraded, and
neither is one that only automation reaches. A `captured` line counts even after
`imprint spool prune` has removed its envelope. A fresh spool file counts too, and
`imprint capture` or another node can write one, so while they do the Stop hook can
capture nothing without this reason; `hook_route_outcomes_24h.stop_capture` still
shows it. The window is read from the newest 2 MiB of the route log, current and
rotated file, so on a busy store a long window counts only what that tail holds,
which is always far more than 10 lines. Health then also reports
`capture_silence_days`, `capture_silence_stop_calls` (the Stop lines in the window
that could have captured), `capture_silence_captures` (the `captured` ones among
them) and `capture_silence_min_stops`. With the key unset none of these appear and
the reason is never raised. Stop lines exist only from the build that writes them,
so the reason cannot be raised by Stops from before it.

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
- A short line has `authority`, `record_id` and `text`, and `partition` on anything
  but a judgment, at up to 35 bytes. Authority alone cannot keep a declared claim from
  an observed result, or a chosen future from a rule, so that label stays. `text` is
  always the record's own words, and text over 400 bytes is cut. Text that was cut
  adds `text_basis` and `"text_complete": false`, and a line without them has all of
  its text. Picks whose text fits a short line go short even when they would all fit
  whole.
- A pick whose text would be cut is delivered on a whole line instead when that line
  fits beside the short lines of the picks after it, with its `selector` block whole.
  A cut can lose an exception, and there is no reason to lose one on a turn with room.
  It never costs a later pick its line, so what fits on a crowded prompt is unchanged.
  Local records and the fallback are not weighed this way: a long one goes short.
- A captured record is cut by the compact line's rules above, which read an operator's
  message and drop what it quotes or pastes. A short line also carries documents and
  imports, where a quote can be the rule itself, so any other record is cut as it
  stands: its start and its last sentence, with `text_basis` `stored_text`.
- A sentence too long to fit keeps its head and its ending, because one sentence can
  hold a whole quotation and the verdict after it. Compact lines still cut it from
  the start.
- A reserved rule keeps the record's last sentence after its rule, and `text_basis`
  stays `rule_sentences`. A later sentence can overrule the rule, and the reserved
  mark would otherwise vouch for what it overruled. The last sentence takes at most
  half of the 400 bytes, so the rule always has the rest. A compact line says the rule
  alone.
- The first short line of a payload also has `"detail": "imprint details
  <record_id>"`, which holds for every line in it.
- `selector` appears only on a reserved rule, as `{"reserved": true}`. Nothing reads
  the rest of the block: the order of the lines is the rank, and the decision log
  has each pick's score.
- `imprint details <record_id>` returns what a short line leaves out: one record's
  whole retrieval line and nothing else, with `case_ids`, `case_summaries`,
  `evidence_ids`, `ontology` and `source_receipt_ids`. The data root must admit
  SQLite's side files, because the store opens in WAL mode. An id with no such line
  fails with `unknown record` and exit code 2, whatever the reason, so a caller cannot
  tell a node retrieval never delivers, such as Evidence, a Case or a Call, from a
  record closed since, a Verdict whose Case or Evidence closed, a record retrieval
  cannot read or an id the store does not know. A record awaiting review does answer,
  with `authority` `inferred`, `extracted` or `imported_floor`, and a node of type
  `Proposal` does not. Nothing limits the command by client yet. Only the record asked
  for is read that way, so one bad record does not stop the lookup of another. A store that cannot be read says so with
  its own error, not `unknown record`, so a live record never looks gone. That includes
  a row whose stored JSON cannot be parsed, which stops every lookup.
- `imprint history <record_id>` is meant for the operator, because one call returns
  the whole capture behind a record. Nothing enforces that yet, and a compact line's
  `detail` still points at it. Its `whole_line` is the line `imprint details` returns,
  or `null` for a node retrieval never delivers, a record closed since or a record
  retrieval cannot read, whose `versions` and `dispositions` say why. A Verdict whose
  Case or Evidence closed keeps its `whole_line` here, though `imprint details` fails
  on it.
- In `jev` mode, a rule whose whole text went out on a short line counts as
  delivered, so no turn Jev owns, and no fallback from one, sends it again. One whose
  text was cut is listed in `retry_ids`, and when it is picked again it arrives on a
  whole line, as long as there is room, and is then done. Session start and the
  fallback list every record they cut, because Jev's budget can be larger than theirs.
  A pick whose whole line can never fit Jev's budget counts as delivered from its
  short line, which can be its second time out cut. So does a local record, cut or
  not, because nothing picks it and it would otherwise go out cut on every turn.
- Only a pick can send a cut record whole, so in `jev` mode nothing else sends one
  again. The deterministic fallback keeps out records still owed a whole line as well
  as those delivered, and a turn Jev owns keeps them out of its local records. A cut
  record therefore goes out once in an outage, however often the store moves, and its
  place goes to a record not yet sent. On whole lines the fallback can still deliver
  a compact pick, because there it has the whole line to send.
- In `deterministic` and `shadow` mode, and on a T3 scratch turn, which takes the
  deterministic answer, nothing reads the session's delivered records or `retry_ids`.
  A receipt holds for one store snapshot, as it does on whole lines, so a new snapshot
  sends the matched domain again, and a record cut there stays cut.
- Once a section has refused a line, the rest of its records over 400 bytes are
  passed over without being cut, because cutting reads all of a long record and
  session start meets every eligible one. Shorter records can still fill the room.

With a 56-byte record id, 160 bytes of plain text and a budget of 4,096 bytes, 15
rules fit a prompt and 11 fit session start, and that is the best case: 15 beside two
reserved rules leaves 9 bytes of the payload. Expect about 14 on a prompt and 10 to 11
at session start until live receipts give the real figure. JSON escapes quotes and
line breaks, so 160 bytes of ordinary prose holds 14 and 11. Text of 170 bytes holds
14 and 10, and 200 bytes holds 13 and 9. A prompt of nothing but `imported_floor`
rules holds 14, and so does one with 5 reserved rules. Text cut to 400 bytes costs
more than these. Session start reaches 12 only with plain text of 146 bytes or less:
12 lines and the lookup have 252 bytes a line, and the line's keys and id take 106 of
them.

Both keys are dotted, so an older build keeps them and reads nothing into them, and
rolling back needs no change to the config.

## Client-work carrier queue

`imprint carrier codex` reads one Codex UserPromptSubmit event on stdin. It is fixed
code: no model and no network. It prints nothing and always exits 0, so it can never
block a prompt. It does nothing at all until `org.imprint.client_carrier.queue` is
set. No hook calls it yet; registering one is a separate, deliberate step.

When the queue is set, it appends one row for a prompt that is client work:

- The thread must be one a person started. Codex's own thread record is read from
  `$CODEX_HOME/state_5.sqlite` (default `~/.codex`), opened read-only with a short
  wait for a lock. The record must say a user started it from the desktop app or the
  CLI, with no spawn edge and no agent field. A subagent, automation, scripted run,
  or unreadable record makes no row, and so does harness or automation text.
- Up to three signals name a client, each from the client list: the starting folder
  is inside one of the client's folders (the deepest match wins), the thread's name
  starts with one of its prefixes, or the prompt matches one of its strong name
  patterns. Two kinds of signal naming the same client mark client work for that
  client. One signal, or signals naming different clients, mark the client unknown,
  and nothing is guessed. No signal means it is not client work and nothing is
  written. Weak patterns are recorded in the row but never count. Only rooms of kind
  `client` or `client_rules_core_agents` are clients.
- A client person's first name, as a whole word in its written case, is a backing
  signal. It counts as a second, agreeing signal only beside another signal for the
  same client (folder, prefix, or a strong pattern such as a domain or full name).
  Alone, or with only a weak pattern, it makes nothing. A first name that people of
  two rooms in the list share, or that belongs to a person who spans rooms, counts
  for no room. A first name of another client than the other signals marks the
  client unknown. A first name inside a name or address a pattern already matched
  is not counted again.
- Paste shape is recorded as flags (`colon_lead`, `rule_run`, `timestamp_lines`,
  `slack_name_time`, `image_attachment`) and is never a client signal.

The row is a pointer: `schema`, `thread_id`, `message_id` (the turn id), `text_sha256`,
`time`, `shape_flags`, `client` (`status`, `guess`, `reason`, `signals`), `asks`,
`loom_ids`, `image_paths`, `speaker_guess`, `channel`, `carrier` and `round`. It holds
no word of the prompt. `asks` lists what a card must ask: `client` when the client is
unknown, and `speaker` when the prompt has no feedback-shape flag, so whether it is a
paste, a paraphrase or the person's own words is unknown. It is empty when nothing
needs asking. The queue file is created with mode 0600 and is refused when it is a link,
relative, in a cloud-synced folder (the home `Documents` and `Desktop` folders count,
because iCloud can sync them whole), inside any git repository, or inside the session's
starting folder, the operator's data root, the Codex home or the transcript's folder.
The queue is never
the store and never a spool, and nothing in it reaches retrieval.

Both keys are dotted, so an older build keeps them and reads nothing into them.

## Environment overrides

- `IMPRINT_CONFIG`: config path.
- `IMPRINT_SELECTOR`: any non-empty value other than `jev` or `shadow` (for
  example `off`, `deterministic`, `0`, `false`, or `disabled`) forces
  deterministic retrieval over any config. `jev` and `shadow` leave the
  configured mode in force. It can only turn the selector off; no value turns
  it on. It is read from the process that sends, so set it where the hook will
  see it: under T3, the instance's `environment`, the home's `env` or the server's
  environment. A shell export reaches only what that shell runs.
- `IMPRINT_DECIDE`: any non-empty value turns every typed-decision purpose off; see
  Typed decisions. No value turns one on. `IMPRINT_SELECTOR` values that turn the
  selector off turn the purposes off too.
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
