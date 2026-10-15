# Changelog

Releases are annotated git tags `vYYYY.MM.DD`, signed with the release key whose
public part ships in `bin/ccsync_lib/release-signers`. The engine reads the tag's
note from this file: the section heading must be exactly the tag.

## v2026.10.15

First signed release. The engine can now update itself (`/sync-update`).

- **Engine updates.** `ccsync.py engine update | rollback | status | check | mode`
  and the `/sync-update` command. Files you edited are left alone, everything replaced
  is backed up, a release whose code does not start is rolled back automatically, and
  only the engine's own paths are committed. Modes: `notify` (default), `auto`, `off`.
  A vault older than this release installs it once with `--adopt`, see the README.
- **Sessions are no longer lost on sync.** `pull` and `push` merge transcripts by record
  `uuid` instead of replacing one copy with the other, so a session written on two
  machines keeps every record; session titles are carried into the tail of the file.
- **Large sessions.** The copy sent to the vault is slimmed (images, service records,
  tool outputs, history) to stay under GitHub's 100 MB file limit; `ccsync.py slim` and
  `restore-session` are new.
- **Fixes.** Records containing U+2028 are no longer corrupted; temp files from
  interrupted writes are ignored by git; automatic `sync from …` commits no longer
  sweep up unfinished engine edits; `pull` survives a project directory that is not
  reachable on this machine.
