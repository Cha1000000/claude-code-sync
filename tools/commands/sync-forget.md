---
description: Forget a session everywhere — remove it from the vault and every machine (irreversible)
argument-hint: "[session id — current session by default]"
---

Removes a session from everywhere: marks it non-syncable, deletes the copy in
the shared vault, leaves a tombstone so the other machines drop their copies on
their next `pull`, and deletes the local transcript.

The current session:

```bash
python3 ~/claude-code-sync/bin/ccsync.py forget --yes
```

A specific one, when an id is given ($ARGUMENTS):

```bash
python3 ~/claude-code-sync/bin/ccsync.py forget --yes --session $ARGUMENTS
```

**This cannot be undone.** Before running it, make sure the user wants the
session forgotten rather than simply no longer sent — for the latter there is
`/sync-ignore`, which deletes nothing.

What to know and to report afterwards:

- When forgetting the **current** session, its local file is not removed right
  away but after the session closes: Claude Code is writing to it at this very
  moment and would recreate it. Say so — the session disappears from `/resume`
  once the user exits.
- The copy is removed by an **ordinary commit**; git history is not rewritten.
  In clones made earlier, the old commits still carry the transcript. Do not
  leave that unsaid.
- Memory facts are untouched. If this session wrote something into
  `memory/facts/`, offer to remove that separately.
- To keep the transcript locally and drop it only from the vault and the other
  machines, add `--keep-local`.
