---
description: Bind a project to its path on this machine
argument-hint: "<project-key> [path]"
---

The same project lives at unrelated paths on different machines, so the mapping
is stated explicitly:

```bash
python3 ~/claude-code-sync/bin/ccsync.py bind $1
```

With no path the current directory is used. If the project key is unknown, check
`/sync-status` first — unbound projects are listed there.

After binding, offer to run `/sync-pull session` so this project's sessions are
laid out at the right path.
