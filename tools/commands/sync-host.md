---
description: Host scripts and systemd units that serve Claude Code, and their scope
argument-hint: "[add <path> | <key> [--here|--not-here|--global]]"
---

Show the host files the vault carries and where they apply:

```bash
python3 ~/claude-code-sync/bin/ccsync.py host
```

If the user asks to take a new file under sync:

```bash
python3 ~/claude-code-sync/bin/ccsync.py host add ~/.local/bin/name.sh
# the default scope is the current OS only; shared everywhere:
python3 ~/claude-code-sync/bin/ccsync.py host add ~/.local/bin/name.sh --global
```

If the user named a file and what to do with it, set the scope (a key such as
`bin/name.sh` or `systemd/name.timer`):

```bash
python3 ~/claude-code-sync/bin/ccsync.py host scope <key> --here
python3 ~/claude-code-sync/bin/ccsync.py host scope <key> --not-here
python3 ~/claude-code-sync/bin/ccsync.py host scope <key> --global
python3 ~/claude-code-sync/bin/ccsync.py host scope <key> os:linux
```

Send the changes with `ccsync.py push tools`. The other machines pick the files
up on their next `pull` — scripts get their `x` bit back, units with an
`[Install]` section are enabled for you.

Comment on the result in plain language. Worth noticing:

- **only what serves Claude Code travels.** `~/.local/bin` and
  `~/.config/systemd/user` also hold the rest of the machine's life — cloud
  mounts, clipboard sync, tray icons. None of that belongs in a shared vault, so
  do not suggest adding it;
- `правлен здесь руками` ("edited here by hand") — the local copy diverged from
  the vault and `pull` left it alone. Either send yours (`push tools`) or delete
  the file and take the shared one again;
- `ещё не отдан — уедет при push` ("not sent yet — goes with the next push") —
  the file is registered but not in the vault yet;
- the `systemd` category only ever applies on Linux, whatever the scope: macOS
  schedules through launchd, Windows through Task Scheduler.
