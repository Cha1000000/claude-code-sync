---
description: Secrets that travel between machines encrypted (age)
argument-hint: "[add <path> | add-recipient]"
---

Show which secret files the vault carries and who can decrypt them:

```bash
python3 ~/claude-code-sync/bin/ccsync.py secrets
```

If the user asks to take a new file with a key under sync:

```bash
python3 ~/claude-code-sync/bin/ccsync.py secrets add ~/.claude/name.key
# the default scope is the current OS only; shared everywhere:
python3 ~/claude-code-sync/bin/ccsync.py secrets add ~/.claude/name.key --global
```

If this is a new machine that must be able to decrypt the shared secrets:

```bash
# its own key first, if there is none yet
age-keygen -o ~/.claude/ccsync-age.key && chmod 600 ~/.claude/ccsync-age.key
python3 ~/claude-code-sync/bin/ccsync.py secrets add-recipient
python3 ~/claude-code-sync/bin/ccsync.py push tools   # re-encrypt for the new recipient
```

Comment on the result in plain language. Worth noticing:

- **encryption and decryption happen on their own** inside `push tools` /
  `pull tools` — nothing else needs to be called;
- `✓` — the file is present here; `·` — it applies here but is missing locally
  (it arrives with the next `pull`, provided the key is here);
- **the private key `~/.claude/ccsync-age.key` never enters git.** It is carried to
  a new machine by hand — or that machine generates its own key and its public
  part is added with `add-recipient`. The second way is better: no single secret
  is shared by every machine;
- after `add-recipient` the files must be **re-encrypted** (`push tools`),
  otherwise the new machine cannot decrypt them — it is not in the ciphertext yet;
- if `pull` says the secrets cannot be decrypted, this machine has no key or `age`
  is not installed (`pacman -S age`, `apt install age`, `brew install age`);
- a local secret you did not touch is **updated** by `pull`; one that was changed
  right here is **not overwritten** — you are told about the difference instead.
  To send your version: `push tools`. `push` encrypts only what changed here, so a
  stale copy never overwrites a newer key from another machine.

Secrets are the only thing stored encrypted. The rest of the vault is plain
text, so keys still must not be written into ordinary files (memory, templates,
configs).
