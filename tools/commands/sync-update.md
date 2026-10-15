---
description: Update the ccsync engine from a new release of the public repository
argument-hint: "[--tag vYYYY.MM.DD] | mode [notify|auto|off] | rollback"
---

Update the sync engine. First look at the plan without writing anything:

```bash
python3 ~/claude-code-sync/bin/ccsync.py engine update --dry-run $ARGUMENTS
```

Tell the user in plain words: which release is installed and which one is coming,
how many files are new, updated or removed, **which files they edited by hand and
will stay as they are**, and whether the release signature checks out. If the
signature says "НЕ СХОДИТСЯ" (does not match), installing is not allowed — say so
and stop. If there is no plan ("уже на этом выпуске", already on this release),
say that.

Only after an explicit yes from the user:

```bash
python3 ~/claude-code-sync/bin/ccsync.py engine update --yes $ARGUMENTS
```

The engine backs up what it replaces, checks that the new code starts (otherwise
puts the old one back), commits only its own paths and pushes. Afterwards remind the
user to restart Claude Code and, if the report mentions `setup-hooks.py`, to run it.

Other modes (by argument):

- `mode [notify|auto|off]` — `python3 ~/claude-code-sync/bin/ccsync.py engine mode <mode>`:
  `notify` (default) tells you about a new release, `auto` installs signed releases
  by itself, `off` never checks;
- `rollback` — `python3 ~/claude-code-sync/bin/ccsync.py engine rollback` (`--list` shows backups);
- status — `python3 ~/claude-code-sync/bin/ccsync.py engine status`.
