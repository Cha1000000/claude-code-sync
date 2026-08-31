---
description: Keep this session off the vault — the transcript stays on this machine
argument-hint: "[reason]"
---

Marks the current session private: its transcript will no longer travel to the
shared vault, neither by a manual `push` nor by the background hooks.

```bash
python3 ~/claude-code-sync/bin/ccsync.py ignore --reason "$ARGUMENTS"
```

The mark is tied to the session id and stays forever: reopening the same session
through `/resume` will not sync it either.

Nothing is deleted. If the session has been running for a while, a copy has most
likely already left — the command says so directly. In that case tell the user
that the mark now applies going forward, but only `/sync-forget` removes the
copy that already left, and ask whether to do that.

List the marked sessions: `ccsync.py ignore --list`.
Undo a mark: `ccsync.py ignore --undo <id>`.
