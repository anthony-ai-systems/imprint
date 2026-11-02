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

## Cloud-sync root refused

Move the data root to a local non-synchronized directory and update config. A
manual bypass would reintroduce concurrent-writer and partial-sync corruption;
the permanent fix is to keep the canonical database local and move immutable
exchange artifacts separately.
