# Adding another machine

This page is for your **second and further** machines — the vault already exists
and has something in it. Setting up the very first one is in
[README](README.md#getting-started-first-machine).

Copy the text below and paste it into Claude Code on the new machine. There are
no requirements for how that machine is laid out: one where none of your projects
exist yet connects just as well.

*[Русская версия](BOOTSTRAP.ru.md)*

---

## Prompt for Claude Code

```
Connect this machine to my shared Claude Code vault. Work step by step and show
me the result of each one.

1. FIRST update Claude Code itself, before anything else:
       claude --version
       claude update
       claude doctor
   The session transcript format changes between releases, so the machines have
   to be on close versions: an older build may fail to read a session that came
   from a newer one.

   In the `claude doctor` output, look at two lines and show them to me:
     - `Config install method:` — if it is not `native`, Claude Code was
       installed by the system package manager and `claude update` will not work
       there: update it the way that OS expects (pacman/apt/brew/winget).
     - `Auto-updates:` — should be `enabled`. If it says `disabled`, check
       whether DISABLE_AUTOUPDATER is set (in the environment, in the env block
       of ~/.claude/settings.json, in the shell profile) and remove it so the
       machine keeps itself current.

   Do not touch `autoUpdates` in ~/.claude.json: for a native install it is
   unused and may read false while auto-updates work fine. The source of truth
   is the `Auto-updates:` line from doctor.

   Show me the version before and after, and only continue once it is updated.

2. Check that git, gh (or whatever hosts my vault) and python3 (3.9+) are
   installed. Install whatever is missing the way this OS expects, and tell me
   what you installed.

3. Make sure git can reach my repository. With GitHub that means `gh auth status`
   and, after logging in, `gh auth setup-git` — authorising `gh` alone is not
   enough: until the credential helper is configured, `git pull` and `git push`
   inside the engine fail with `could not read Username for
   'https://github.com'` even though `gh auth status` looks healthy.

   Also check `git config --get user.email` and `--get user.name`: without them
   `ccsync push` cannot commit.

4. If Claude Code was already used on this machine (there is a ~/.claude with
   skills, memory, sessions), FIRST make an archive, before any ccsync step:
       tar czf ~/claude-backup-$(date +%Y%m%d).tar.gz -C ~ .claude $(cd ~ && ls .claude.json 2>/dev/null)
   (on native Windows use `tar` from PowerShell with the date typed in, or just
   copy the folders). Show me the archive's path and size. On a clean machine,
   skip this paragraph.

   Clone my vault to this exact path — the slash commands reference it:
       git clone <my repository url> ~/claude-code-sync

5. Create this machine's passport:
       python3 ~/claude-code-sync/bin/ccsync.py init
   The script proposes an id — show me its suggestion and let me confirm or
   change it. The id should be recognisable (mac-studio, win11-laptop,
   ubuntu-thinkpad) and must not repeat an existing one; the list is
   `python3 ~/claude-code-sync/bin/ccsync.py machines`. In the machine note,
   describe briefly what this machine is.

6. Tokens. If the vault carries encrypted secrets (the list is
   `python3 ~/claude-code-sync/bin/ccsync.py secrets`), this machine needs its
   own age key to decrypt them:
       age-keygen -o ~/.claude/ccsync-age.key && chmod 600 ~/.claude/ccsync-age.key
       python3 ~/claude-code-sync/bin/ccsync.py secrets add-recipient
       python3 ~/claude-code-sync/bin/ccsync.py push tools
   Then tell me to run `/sync-pull tools` and `/sync-push tools` on a machine
   that already decrypts — that re-encrypts the files for this one too. Until
   then `pull` reports the secrets as locked, which is expected. If age is not
   installed, install it first. If the vault carries no secrets, nothing is
   needed here: if step 8 reports a missing one, name it to me and I will put
   the value into ~/.claude/ccsync-secrets.env myself.

7. Take the state:
       python3 ~/claude-code-sync/bin/ccsync.py pull all
   For projects that are not bound here the script asks for a path. If a project
   does not exist on this machine, skip it — its sessions go to
   ~/claude-sessions/<key> and will still open.

7a. ONLY if Claude Code was used on this machine before. The sync hooks start
   working only after the restart in step 10, so until then nothing leaves this
   machine by itself — this is the window to sort out its previous state. Go
   through it together with me, do not decide for me:

   a) MCP servers: `python3 ~/claude-code-sync/bin/ccsync.py mcp`. Servers marked
      "only here, not in the vault yet" go to EVERY machine on the first push.
      Show them to me; the ones tied to this machine (its paths, its programs)
      get marked before the push: `ccsync.py mcp scope <name> --here`.
   b) Skills and commands: compare `ls ~/.claude/skills ~/.claude/commands` with
      `~/claude-code-sync/tools/skills` and `tools/commands`. Show me what exists
      only here: it goes to every machine on push. Remove what is not wanted
      before pushing.
   c) Memory. `pull` kept the previous index as MEMORY.md.bak (next to the new
      MEMORY.md, in ~/.claude/projects/<home directory slug>/memory/); the old
      note files are there too. Other projects have memory as well —
      `ls ~/.claude/projects/*/memory`. Go through it with me, one note at a time:
        - about me, my work, my projects: the vault's version is newer — move
          over only what it lacks (extend an existing fact or add a global one);
        - about this machine (its paths, software, settings): a new fact scoped
          to this machine, or to `os:darwin` / `os:win32`;
        - outdated — leave it out; it stays in the archive from step 4.
      New facts follow the rules in the ccsync block of ~/.claude/CLAUDE.md.
      `ccsync adopt` is NOT the tool for memory here: on any machine but the
      vault's first it only moves notes that already have a scope and lists the
      rest — those get scoped by hand.
   d) Then: `python3 ~/claude-code-sync/bin/ccsync.py adopt` (skills/commands/…
      become symlinks into the vault; the previous copies go to
      ~/.claude/backups/) and `ccsync.py push all`.

8. Tell me which secrets are missing, if any, and which plugins need installing —
   the script prints a ready `claude plugin install ...` command.

9. Check that everything landed:
       python3 ~/claude-code-sync/bin/ccsync.py status
       python3 ~/claude-code-sync/bin/ccsync.py machines
       python3 ~/claude-code-sync/bin/ccsync.py mcp
   The last one shows the MCP servers and their state here. A server marked
   "WILL NOT START" either gets installed or gets marked as not needed on this
   machine: `ccsync.py mcp scope <name> --not-here`.
   Confirm separately that ~/.claude now has the skills, commands, hooks and
   plans symlinks, and that MEMORY.md has an "About this machine" section —
   empty at first, which is normal; facts about this machine appear later.
   Compare the Claude Code version with the other machines — `machines` prints
   it for each, and `status` warns if one of them is newer than this.

10. Restart Claude Code so the hooks are picked up, and check that a new session
   starts with a [ccsync] Machine: ... block.

Note: ~/.claude/ccsync-machine.json and ~/.claude/ccsync-age.key are never
synced — each machine has its own. Do not copy them from another machine.
~/.claude/ccsync-secrets.env travels only if it was registered as a secret, and
then only encrypted.
```

---

## What happens

| Step | Result |
|---|---|
| `init` | Creates `~/.claude/ccsync-machine.json` and registers the machine in `machines/<machine>.json` |
| `pull all` | Symlinks for skills/commands/hooks/plans, `CLAUDE.md` and `statusline.py` if the vault carries them, `settings.json` merged for local paths, MCP servers, host scripts and systemd units, encrypted secrets decrypted (if this machine has a key), a local `MEMORY.md` by scope, session transcripts |
| Hooks | `SessionStart` pulls and prints where you are, `Stop` and `SessionEnd` push |
| Check | `tools/tests/run-all.sh` — the test rigs; worth running once on a fresh machine to confirm the engine works there |

## A machine that already had Claude Code

Connecting such a machine does not harm the shared vault: `pull` takes the
vault's skills, `CLAUDE.md` and settings, and the first `push` afterwards sends
only what was changed on this machine after that `pull` (the
`tools/tests/first-join.sh` rig checks exactly this). What changes is the machine
itself, and its previous state is kept next to it:

| What the machine had | What it gets |
|---|---|
| `CLAUDE.md`, `settings.json` | the shared ones; previous copies — `CLAUDE.md.bak`, `settings.json.bak` |
| `MEMORY.md` | rendered from the shared facts; previous one — `MEMORY.md.bak` |
| Memory notes | stay on disk; into shared memory only by hand, with a scope (step 7a) |
| Skills and commands with the same name | the newer one by modification time — usually the vault's |
| Skills, commands, MCP servers the vault lacks | go to every machine on the first `push` — hence step 7a |
| Old sessions | stay on the machine; only the current one is pushed |

**Settings.** The first `pull` **replaces** an existing `settings.json` rather
than merging it: the three-way merge that protects your local keys needs a
baseline to compare against, and that baseline only exists from the second pull
onwards. So if this machine has settings worth keeping (a different model, a
theme, an extra hook), open `settings.json.bak` afterwards, move what you want
back into `settings.json`, and run `ccsync.py push tools` — from then on your
machines share one set of settings and local edits survive every pull.

## Per-OS notes

**Windows.** Both native and WSL are supported; choose by where your code lives,
not by what is convenient for syncing.

What was done so the native path works without caveats:

- No hook contains a shell command. They all call `bin/cchook.py`, which does the
  timeout, the backgrounding and the error suppression in Python. The interpreter
  and the vault path are substituted per machine at `pull` time (they are
  `{{PYTHON}}` and `{{VAULT}}` in the template), and paths with spaces are quoted
  whole.
- Settings and MCP entries are assembled node by node rather than by text
  substitution: a path like `C:\Users\alex` pasted into a JSON string would be an
  invalid escape sequence and would break the whole file.
- Symlinks are always attempted (with Developer Mode they work on Windows too),
  and where unavailable copies are used — `push` then collects edits back out of
  those copies, so a skill added on such a machine is not lost.
- Separators and drive letters (`D:\Projects\…`) are handled when sessions move.
- Output is switched to UTF-8, otherwise the Windows console trips over
  non-ASCII characters.

**Choose WSL** if on this machine you mostly talk to Claude and make small edits:
then `$HOME` matches the Linux one and the machine behaves like any Ubuntu
laptop. **Choose native** if real development happens here in an IDE and the code
lives on `D:\` — from WSL it is visible as `/mnt/d`, and git over that path is
noticeably slower.

**macOS.** Claude Code keeps its credentials in the Keychain rather than in a
file, so a separate login may be needed after connecting. That is normal and has
nothing to do with syncing.

## When a project lives at a different path

Paths to the same project are unrelated across machines, so the mapping is stated
explicitly, once per machine:

```bash
cd /path/to/the/project/on/this/machine
python3 ~/claude-code-sync/bin/ccsync.py bind <project-key>
python3 ~/claude-code-sync/bin/ccsync.py pull session
```

`status` lists the project keys. The binding travels to the vault — no need to
repeat it on the other machines.
