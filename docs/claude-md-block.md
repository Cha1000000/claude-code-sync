# Block for your CLAUDE.md

Paste this into your own `~/.claude/CLAUDE.md`. It tells Claude that memory is
now shared between machines, and how to keep it that way.

The vault deliberately ships **no** `CLAUDE.md` of its own: `pull` overwrites
that file whenever the vault has one, and your instructions must not be
overwritten by a template. Your first `push` picks up the merged file and
carries it to your other machines.

Replace `~/claude-code-sync` below if you cloned the vault elsewhere.

---

```markdown
# Shared state across my machines (ccsync)

This file, my skills, commands, memory and session transcripts are synced
between all my machines through a private git repository at
`~/claude-code-sync`. There are several machines, on different operating
systems, and the list is open-ended.

## Where you are right now

At the start of every session a hook prints a `[ccsync] Machine: …` block.
**That is the answer to "which machine is this".** Do not work it out again
from `uname`, `hostname` or the contents of directories — read it from that
block. The machine's passport lives in `~/.claude/ccsync-machine.json` and
never goes into git.

Memory is shared, so it holds facts about **all** the machines at once. The
local `MEMORY.md` is already filtered for the current one: the "common" and
"this machine" sections apply here, the "other machines" section does **not**.
Never carry a decision over from there without checking that it fits this OS.

## Writing new facts

Fact files live in `~/.claude/projects/<slug>/memory/facts/` (a symlink into
the vault). `MEMORY.md` is **generated** — editing it by hand is pointless, the
next `pull` overwrites it.

Every new fact needs these fields in its frontmatter:

    metadata:
      type: user | feedback | project | reference
      scope: global                  # true on every machine
      index_title: "Short title for the index"
      index_hook: "The gist in one line — what used to go into MEMORY.md"

Values for `scope`:

- `global` — about me, my work, my projects, my home infrastructure, how I want
  you to work, how the tools behave;
- `<machine-id>` (for example `linux-desktop`) — installed software, drivers,
  desktop settings, local environments and paths of **one specific** machine;
- `os:linux` / `os:darwin` / `os:win32` — true for any machine on that OS;
- a list: `[linux-desktop, work-laptop]`.

**When in doubt, use a machine scope rather than global.** A wrong `global`
spreads local knowledge to every machine; a wrong machine scope only makes me
ask again.

## MCP servers have scopes too

By default a server is shared: install it here, it appears everywhere. But if it
depends on a local database, a self-built binary or one particular OS, mark it
straight away, without putting it off: `/sync-mcp <name> --not-here` (everywhere
except this machine) or `--here` (only here). Otherwise it travels to the other
machines and fails silently there at every session start.

## Commands

- `/sync-push [all|session|tools|memory]` — send this machine's state
- `/sync-pull [all|session|tools|memory]` — receive and lay it out here
- `/sync-status` — what differs
- `/sync-bind <key> [path]` — bind a project to its path on this machine
- `/sync-mcp [name] [--here|--not-here|--global]` — MCP servers and their scope
- `/sync-ignore [reason]` — keep this session out of the vault
- `/sync-forget [id]` — forget a session everywhere, here and on every machine

Normally none of them are needed: `SessionStart` pulls, `Stop` and `SessionEnd`
push.

## Private sessions

If I say a conversation must not travel to the other machines, that is
`/sync-ignore`, and the mark goes on **immediately**, not at the end of the
session: the background `Stop` hook sends the transcript every few minutes, so
everything said before the mark is already in the vault. If that happened, only
`/sync-forget` removes what left — it also deletes the copies on the other
machines.

`/sync-forget` is irreversible and deletes the local transcript too (for a live
session, once it closes). Do not reach for it when I only asked to stop syncing.

## What not to do

- Never write secrets into vault files. Tokens go in
  `~/.claude/ccsync-secrets.env` only (local, git-ignored).
- Never edit `MEMORY.md` or `memory/index.md` — both are generated.
- Never put absolute machine paths into vault files by hand: paths are
  tokenized automatically (`{{HOME}}`, `{{P:key}}`).
- Never edit the `machines/` and `project-map/` registries on behalf of another
  machine: each one writes only its own file, and that is exactly why merges
  never conflict.
```
