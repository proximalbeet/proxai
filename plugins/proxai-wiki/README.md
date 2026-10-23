# proxai-wiki: the wiki synthesizer

Turns new and changed vault notes into pages in `10-Wiki`, ProxAI's long-term memory. Replaces the
old obsidian-wiki tool (gone from this PC); keeps its page format, `index.md`, `hot.md` and `log.md`.

Decided with Nathan 2026-10-23 (the "middle option"):

| Notes from | What happens |
|---|---|
| 02-Projects, 03-Courses, 04-Knowledge, 08-Roles, 09-Career | written into the wiki right away |
| 05-Brain-Dump, 00-Daily, 06-Focus-Logs | written on the side; one digest waits for your **yes** |
| 01-Personal, 07-Security, 10-Wiki, _templates, _needs-review, _inbox | never read |
| any note with `wiki: no` in its frontmatter | skipped |

Folders, batch size (20 notes per run), the nightly time (03:30) and the models are in
`policy.json`; changes apply on the next run.

## Using it (phone, DMs or CLI)

- "update the wiki": runs now, in the background (a few minutes).
- "wiki status": last run, what's waiting, the last change.
- A waiting update is mentioned once in your next chat; say **yes**, or "approve the wiki update" /
  "reject the wiki update". Rejected notes come back only if you edit them.
- "undo the last wiki update": puts 10-Wiki back as it was before the last change.
- Nightly at 03:30: same as "update the wiki".

## How it works

1. **Find new notes** (the plugin, on the PC): reads the policy folders, skips unchanged notes
   (`~/.local/state/proxai/wiki/state.json` keeps a hash per note).
2. **Workspace**: `~/dev/proxai/wiki-work` (`/workspace/proxai/wiki-work` in the sandbox) is a git
   copy of `10-Wiki`, synced from the vault before each run (your own edits in Obsidian are kept).
3. **Claude Code** (Sonnet; Opus if the Sonnet run fails) reads the notes from the read-only vault
   and writes pages in the workspace, following the guide in `__init__.py` (`GUIDE`): page folders,
   frontmatter like the existing pages, `[[links]]`, `index.md`, `hot.md`, a `log.md` line per note,
   and a short digest. No personal details; never deletes or renames pages.
4. **Commit**: each run is one git commit. Auto notes on `main`, review notes on a `review` branch.
5. **Promote**: `promote.py` runs as its own sandboxed systemd job that sees only `10-Wiki`, the
   workspace (read-only) and its state folder, no network. It copies just the files the commit
   changed, backs up anything it replaces or removes (`~/.local/state/proxai/wiki/backups`), and
   takes the shared vault lock (refiler, vault sync, vault writes).
6. **Approve** a review: rebased onto the newest wiki (`index.md`/`hot.md`/`log.md` merge line by
   line); if a page changed on both sides, Claude Code writes those notes again on top.
7. **Undo**: `git revert` of the last promoted change, promoted the same way.

The sandbox now mounts `10-Wiki` read-only, so Claude Code (or chat) can't write the wiki around
these steps. `vault_write` can still write there on purpose (logged).

Logs: `~/.local/state/proxai/wiki.log`, `WIKI` lines in the security log, `10-Wiki/log.md`.

## Tech stack

Python standard library (plugin + `promote.py`), git in the sandbox, Claude Code CLI through the
claude-code-delegation runner (`cc-run`), systemd user jobs for the vault writes.

## Tests

`python3 plugins/proxai-wiki/test_wiki.py` (real git + real promote on a throwaway vault, Claude
faked); `PROXAI_TEST_SYSTEMD=1` also runs promote.py as the real sandboxed systemd job.

## Changelog

- 0.1.0 (2026-10-23): first version.
