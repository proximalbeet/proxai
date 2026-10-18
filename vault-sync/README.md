# Vault auto-sync (Phase 8)

Keeps `~/Documents/ProxVault` (desktop, Hermes) and the laptop in step through the private
GitHub repo `proximalbeet/ProxVault`, without merge conflicts piling up.

## What a run does

A systemd user timer runs `vault_sync.py` every 10 minutes (3 min after boot), right after the
refiler. Each run:

1. Takes `~/.local/state/proxai/vault.lock`, the same lock as the refiler, so they never overlap
2. Stops if the repo is mid-rebase/merge or on another branch, and skips the run if a changed
   note was written in the last 15 s (Hermes or the refiler is still writing)
3. Commits local changes (`vault-sync: desktop <date> (<n> files)`); files over 50 MB are left out
4. Fetches and rebases onto `origin/main` with `--autostash`. Never a merge commit, never a force-push
5. Pushes. If the laptop pushed at the same moment, the push is refused and the next run retries

**On a conflict** (the laptop and the desktop changed the same lines of a note) the rebase is
aborted, so every file is exactly as it was. The sync pauses, `#bot-audit-log` gets one ⚠️ line
naming the notes (`01-Personal`/`07-Security` names are hidden), and every later run reports the
same problem without posting again. Once you fix it, the next run posts ✅.

Fix a conflict on the desktop:
```fish
cd ~/Documents/ProxVault
git rebase origin/main        # stops at the conflicting note(s)
# edit the <<<<<<< ======= >>>>>>> blocks, then:
git add <note>; git rebase --continue
```
The next timer run (or `/vault-sync now`) pushes it.

Uses only the deploy key (`~/.ssh/proxvault_deploy`, this repo only) and pinned GitHub host keys
(`github_known_hosts`). The service can't see `~/.git-credentials`, `~/.gitconfig` or the rest of
`~/.ssh` (`ProtectHome=tmpfs`), and runs git with `credential.helper=` so it never falls back to
the plaintext token.

| File | |
|---|---|
| `vault_sync.py` | The sync (Python stdlib + git). `--dry-run`, `--status`, `prepare [--apply]` |
| `test_vault_sync.py` | 11 tests on throwaway repos (bare "GitHub" + laptop + desktop clones) |
| `vault.gitignore` | The vault's `.gitignore`, installed by `prepare` |
| `systemd/proxai-vault-sync.{service,timer}` | Sandboxed oneshot + 10-minute timer |
| `../plugins/vault-sync/` | Hermes side: `vault_sync` tool, `/vault-sync` command, alerts |
| `~/.local/state/proxai/vault-sync.json` | Latest state (read by the plugin) |
| `~/.local/state/proxai/vault-sync.log` | One JSON line per run, full paths (host only) |

## Why conflicts should be rare

- `.obsidian/workspace.json` (window layout, changes on every click) is no longer tracked. It was
  the main conflict source: both computers had changed it
- Hermes writes one new file per capture; it never edits notes the laptop edits. The refiler is the
  only desktop process that edits existing notes (`00-Daily`), and it shares the lock
- The laptop's Obsidian Git plugin folder is ignored, so desktop Obsidian never runs a second syncer
- Still possible: you edit the same daily note on the laptop while the refiler adds goals to it.
  That stops cleanly and reports; see the decision below if it happens often

## Decisions (Nathan)

1. **Security log on GitHub: yes** (Nathan, 10-18: the repo is private and the container can't
   read `07-Security`). `07-Security/ProxAI-Audit/` syncs like any note. It's append-only and only
   the desktop writes it, so it never conflicts, and it's exempt from the 15 s quiet check (the
   guard writes to it on every message, which would otherwise keep postponing the sync)
2. **Daily notes:** normal merges (default: a same-line edit pauses the sync). If daily-note
   conflicts become common, add `00-Daily/*.md merge=union` to the vault's `.gitattributes`
   (keeps both sides' lines; can duplicate a line)
3. **Hermes and the lock:** today Hermes's own writes don't take the lock; the quiet-period check and
   the clean rebase abort cover it. After Phase 6/7 land, the discord-routing capture writer can
   take the same lock (small change, left out of this branch to avoid merge conflicts)

## Go live (after the Phase 6/7 merges are done)

1. Merge: `git -C ~/.hermes/profiles/proxai merge worktree-vault-sync`
2. Deploy key (this repo only, write access):
   ```fish
   ssh-keygen -t ed25519 -N '' -C proxvault-desktop-sync -f ~/.ssh/proxvault_deploy
   cat ~/.ssh/proxvault_deploy.pub
   ```
   GitHub → proximalbeet/ProxVault → Settings → Deploy keys → Add deploy key → paste,
   tick **Allow write access**. Check:
   `ssh -i ~/.ssh/proxvault_deploy -o IdentitiesOnly=yes -T git@github.com`
   → "Hi proximalbeet/ProxVault! You've successfully authenticated"
3. **Close Obsidian on the desktop**, then the one-time switch-over:
   ```fish
   python3 ~/.hermes/profiles/proxai/vault-sync/vault_sync.py prepare          # shows the plan
   python3 ~/.hermes/profiles/proxai/vault-sync/vault_sync.py prepare --apply
   ```
   It backs up `workspace.json`, commits the desktop's changes, pulls the laptop's, pushes, then
   untracks `workspace.json` and adds `.gitignore`, and puts your desktop layout back. This replaces
   the old "finish the vault pull (`git stash; git pull; git stash pop`)" action. If both computers
   changed the same lines (e.g. `01-Personal/TODO.md`), it stops with nothing changed: fix as above,
   then run `prepare --apply` again
4. Timer:
   ```fish
   ln -s ~/.hermes/profiles/proxai/vault-sync/systemd/proxai-vault-sync.service ~/.config/systemd/user/
   ln -s ~/.hermes/profiles/proxai/vault-sync/systemd/proxai-vault-sync.timer ~/.config/systemd/user/
   systemctl --user daemon-reload; systemctl --user enable --now proxai-vault-sync.timer
   systemctl --user start proxai-vault-sync.service; python3 ~/.hermes/profiles/proxai/vault-sync/vault_sync.py --status
   ```
5. Hermes: `hermes -p proxai plugins enable vault-sync`, add `vault_sync` to `platform_toolsets`
   → `discord` and `cli` in `config.yaml`, add `vault_sync` to the `tool_policy` profiles that should
   use it (e.g. `chat`; DMs need nothing), then `systemctl --user restart hermes-gateway-proxai`.
   `/vault-sync` works in any channel once the plugin is enabled
6. Optional: point the vault's own `origin` at the deploy key, so manual git stops using the token:
   ```fish
   git -C ~/Documents/ProxVault remote set-url origin git@github.com:proximalbeet/ProxVault.git
   git -C ~/Documents/ProxVault config core.sshCommand "ssh -i ~/.ssh/proxvault_deploy -o IdentitiesOnly=yes"
   ```
   Don't delete `~/.git-credentials` yet: other repos (e.g. the proxai profile) still push with it

## Laptop

1. Pull once after step 3 above. Git removes the laptop's `.obsidian/workspace.json` from tracking,
   which deletes the file: Obsidian's layout resets once, then it's per-device for good
2. Obsidian Git plugin (laptop only; its folder is gitignored): Pull on startup **on**,
   Auto pull interval **10**, Auto commit-and-sync interval **10**, Pull before push **on**,
   Sync method **Rebase**. Same `.gitignore` arrives through the pull
3. The laptop pushes with its own login (your normal GitHub auth); the desktop deploy key is desktop only

## Tests

- `python3 vault-sync/test_vault_sync.py` (11 tests, throwaway repos, no network)
- Live: edit different notes on both computers within a few minutes → both appear on both, no
  conflict. Edit the same line of one note on both → `/vault-sync` shows "PAUSED: conflict",
  one ⚠️ line in `#bot-audit-log`, nothing changed on disk; fix it → ✅ line
