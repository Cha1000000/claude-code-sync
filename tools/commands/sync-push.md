---
description: Send this machine's state to the vault — session, memory, tools
argument-hint: "[all|session|tools|memory]"
---

Upload the current state to the shared vault (git) so another machine can pick it up.

```bash
python3 ~/claude-code-sync/bin/ccsync.py push $1
```

Without an argument everything goes: tools (settings, MCP, plugins, skills),
memory, and the transcript of the current session.

Then report briefly what actually left: whose project the session belongs to,
how many memory facts, whether MCP servers or plugins changed. If something was
skipped (a transcript above the size limit, for example), say so plainly instead
of glossing over it.
