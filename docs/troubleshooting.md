# Troubleshooting

## Installer cannot create a virtual environment

Install a complete Python 3.10 or newer distribution with `venv` and `pip`, then rerun
the same artifact installer. On some Linux distributions, `python3-venv` is a
separate OS package. This is a dependency fix; manually copying source files is
not a durable substitute.

## Hook registration fails

The installer fails closed and leaves a timestamped settings backup. Validate
that the settings file is JSON and that its `hooks` value is an object whose
event values are lists. Restore the latest backup if another tool wrote invalid
JSON, correct that writer, and rerun the installer. For a Claude home the
installer did not set up, rerun `manage_hooks.py register` instead; see Hook
registration in `docs/configuration.md`.

## Health is degraded

Read the structured status and counts. Missing config, denied filesystem access,
corrupt spool inputs, a disabled compiler, or database integrity failure require
different fixes. Health reports never include captured text. Preserve corrupt
inputs and the database before repair.

`hook_failures_present` means a file in `logs/hook-failures` is 7 days old or
newer, by its modified time. An older one stops degrading health and stays
counted: `hook_failure_count` is every file, split into
`hook_failure_recent_count` and `hook_failure_old_count`. Health never deletes
one. `imprint spool prune` is the only thing that does, and it is never automatic.

`selector_key_unavailable` means the store is on `selector.mode: jev` and the key
check failed, so every turn falls back to deterministic delivery. Set the key in the
variable `selector.jev.api_key_env` names, or make `selector.jev.api_key_file` a
private regular file you own. A key set only for the hooks reads as missing from
another shell. `selector_failures_repeated` means 3 selections failed in a row with
no picks from Jev. Jev may be down, or selection may be failing on this side before
it can ask or after a reply. `logs/hook-routes.jsonl` gives each turn's `detail`, such as `timeout`,
`http_error` or `missing_key`. A failure after Jev replied, such as a full disk, counts
too: it reads `failed` with `answered` true. The reason clears on the first turn that
uses Jev's answer, or when the failures pass 24 hours. See Hook route log in
`docs/configuration.md`.

A Jev that fails on and off never makes a run of 3, so health stays green. Read
`selector_failure_share` under `metrics`: its `share` is the part of the newest 50
selections in the last day that failed, or `null` with fewer than 10. A share that
stays high with no degraded reason is that pattern, and the route log's `detail` says
which failure it is.

## The agent got no judgments and no notice

With `org.imprint.selector.failure_notice: true`, a prompt turn whose selection failed
and that delivered nothing carries one line naming the reason, such as
`(reason: timeout)`. If a turn got neither judgments nor that line, the turn did not
fail selection: it found nothing relevant, had nothing to send, was a replay, ran under
`shadow` or `deterministic`, or the hook itself died before it could print, which
leaves a file in `logs/hook-failures`. The turn's line in `logs/hook-routes.jsonl`
says which. With the key off, a failed turn emits empty context, as it always did.

## A typed decision sends nothing

`imprint.decide` sends only for a purpose that `org.imprint.decide.purposes` names
with `mode` `shadow` or `enforce` and a `daily_cap` above 0. The result's `reason`
says which check stopped it: `purpose_off` (not named, or `off`), `switched_off`
(`IMPRINT_DECIDE` is set, or `IMPRINT_SELECTOR` turns the selector off, in the
environment of the process that calls), `unknown_purpose`, `cap_reached` (today's
UTC count in `logs/decide-calls.json` reached the cap) or `cap_unavailable` (that file
is not one the module can trust, so delete it to start the day's count again, or
another process held `logs/decide-calls.lock` until the call's deadline). A
`missing_key`, an `invalid_request` or a call already out of time costs no call from
the cap. The purpose's `logs/decide-<purpose>.jsonl` has one line per call that was not
disabled.

A purpose that `imprint health` counts under `metrics.decide_purposes.ignored` is named
in a way this build cannot read: an unknown purpose (often a typo), a key other than
`mode` and `daily_cap`, a mode other than `off`, `shadow` or `enforce`, or a cap that is
not a whole number from 0 to 100,000. It is off, and the config still loads.

`tools/jev_probe/run_probe.py --send-approved-calls 24 --config PATH` prints a
`refused` list and sends nothing until every condition holds: the count is 24, the
config enables `probe` with a cap that leaves 24 calls today, no switch turns it off,
and the key is present. Run it without flags to see the plan and request sizes.

## Cloud-sync root refused

Move the data root to a local non-synchronized directory and update config. A
manual bypass would reintroduce concurrent-writer and partial-sync corruption;
the permanent fix is to keep the canonical database local and move immutable
exchange artifacts separately.
