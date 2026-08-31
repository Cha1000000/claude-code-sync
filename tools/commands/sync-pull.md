---
description: Pull from the other machines — sessions, memory, tools, MCP, plugins
argument-hint: "[all|session|tools|memory]"
---

Take the state from the shared vault and lay it out for this machine.

```bash
python3 ~/claude-code-sync/bin/ccsync.py pull $1
```

Without an argument everything is pulled. Paths inside sessions are rewritten
for this machine automatically; projects that are not bound here land in
`~/claude-sessions/<key>`, and that is not an error.

Afterwards report:

- which sessions became available (they can be opened with `/resume`);
- what changed in the tools (new MCP servers, plugins left to install);
- if any secrets are missing, name the variables to add to
  `~/.claude/ccsync-secrets.env`;
- if a project turned out to be unbound, offer to bind it with `/sync-bind`.
