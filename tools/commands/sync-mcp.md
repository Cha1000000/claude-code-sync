---
description: MCP servers on this machine and which machines they belong to (scope)
argument-hint: "[server-name] [--here|--not-here|--global]"
---

Show the vault's MCP servers and which machines they belong to:

```bash
python3 ~/claude-code-sync/bin/ccsync.py mcp
```

If the user named a server and what to do with it, set the scope:

```bash
# this machine only
python3 ~/claude-code-sync/bin/ccsync.py mcp scope <name> --here
# everywhere except this machine
python3 ~/claude-code-sync/bin/ccsync.py mcp scope <name> --not-here
# back to shared
python3 ~/claude-code-sync/bin/ccsync.py mcp scope <name> --global
# anything else: os:linux, named machines, exclusions
python3 ~/claude-code-sync/bin/ccsync.py mcp scope <name> os:linux
```

After changing a scope, apply it here with `ccsync.py pull tools`. The other
machines pick it up on their next `pull`.

Comment on the result in plain language. Worth noticing:

- a `НЕ ЗАПУСТИТСЯ` ("will not start") line — the server applies here but its
  binary or file is missing. Either install it or mark it `--not-here`;
- `ЕСТЬ локально, правлен руками` ("present locally, edited by hand") — the
  server is scoped away from this machine, but the local entry differs from the
  template, so `pull` leaves it alone. Ask whether to remove it:
  `claude mcp remove <name> -s user`;
- a server tied to a local database, a self-built binary or one specific OS is
  best scoped right away — otherwise it travels to every machine and fails
  silently there at every session start.
