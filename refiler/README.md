# ProxAI vault refiler

Moves notes Hermes writes to `_inbox/` into their vault folder, using the `type` + `area`
frontmatter and the SOUL.md filing table. Runs on the host as a systemd user timer (every 2 min).

- `type: daily` → goals merged into `00-Daily/<date>.md` under "## ✅ Tasks" (note created from `_templates/daily-note.md` if missing)
- `type: focus-log` → gets `date` + `day: "[[date]]"`, renamed `YYYY-MM-DD-HHMM-<task>.md`, moved to `06-Focus-Logs/`
- No frontmatter, unknown type, or `area: personal` → `_needs-review/`
- Never writes to `01-Personal/` or `07-Security/`; skips subfolders like `_inbox/_audit-test/` and notes changed in the last 60 s
- Log: `~/.local/state/proxai/refiler.log`. Backups of every changed or removed note: `~/.local/state/proxai/refiled/<date>/`
- Takes `~/.local/state/proxai/vault.lock`, so it never overlaps the Phase 8 vault sync

## Use

```fish
python3 refiler/refiler.py            # dry run: prints what it would do
python3 refiler/refiler.py --apply    # move files
python3 -m unittest refiler/test_refiler.py -v
```

## Install (after the `_inbox/` test notes are deleted)

```fish
mkdir -p ~/.config/systemd/user ~/.local/state/proxai
cp ~/.hermes/profiles/proxai/refiler/systemd/proxai-refiler.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now proxai-refiler.timer
```
