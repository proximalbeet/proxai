# proxai-vault-write: vault writes with Nathan's permission

Hermes can read the whole vault (except the hidden folders) but write only to a few folders.
This plugin adds one tool, `vault_write`, as the way to write anywhere else, and only with
Nathan's yes.

| Folder | What happens |
|---|---|
| `_inbox`, `05-Brain-Dump`, `10-Wiki` | **free**: written right away |
| `01-Personal`, `07-Security`, `AGENTS.md`, `CLAUDE.md`, anything starting with `.` | **never**: refused |
| everything else (`00-Daily`, `02-Projects`, `04-Knowledge`, …, files at the vault root) | **ask**: Hermes asks first |

Change the lists in `policy.json` (no restart needed). Only `.md`, `.txt` and `.canvas` files, up to
200 KB.

## How asking works

1. Hermes calls `vault_write` with `action=write`. For an "ask" folder nothing is written; the tool
   returns a question, for example *"Can I create 04-Knowledge/rust-notes.md (12 lines). It starts:
   "# Rust notes …"? Say yes to allow."*, and Hermes asks it and stops.
2. Nathan's **next message** decides. A `pre_llm_call` hook (plugin code, not the model) reads it:
   a plain yes ("yes", "yeah do it", "ok", a voice note saying yes) arms the write; anything else
   ("no", "yes but change the title", a question) cancels it. Requests expire after 10 minutes.
3. Hermes calls `action=confirm`, which only works once armed, so the model can't approve its
   own request.
4. Asking only works in Nathan's **Discord DMs, the phone app, or the PC terminal**. Discord
   channels don't get the tool (routing.json `tool_policy`). Scheduled jobs can't write.

`replace` backs up the old note to `~/.local/state/proxai/vault-write/backups/`; `append` adds to the
end; `create` refuses to overwrite.

## How the write happens

The gateway's sandbox keeps the vault read-only, so the plugin starts `apply.py` as its own short
systemd user job (`systemd-run --user --wait`, same idea as the mode switch). That job:

- has its own sandbox: sees only the vault and `~/.local/state/proxai`, with `01-Personal`,
  `07-Security`, `.git` and `.obsidian` hidden, and no network;
- checks the request again (folder rules, approval, file type, size, `..`, symlinks);
- takes the shared vault lock (`~/.local/state/proxai/vault.lock`, same as the refiler and vault
  sync), writes atomically, and logs to `~/.local/state/proxai/vault-write/log.jsonl`.

Every write, refusal and "no" gets a `VAULT-WRITE` line in the security log
(`07-Security/ProxAI-Audit/<date>.md`); approved writes are also posted to #bot-audit-log.
`/vault-writes` shows the rules and the last 5 writes.

## Test

```bash
python3 plugins/proxai-vault-write/test_vault_write.py                          # 22 tests, temp vault
PROXAI_TEST_SYSTEMD=1 python3 plugins/proxai-vault-write/test_vault_write.py    # + the real sandboxed job
```

## Go live (Nathan)

```bash
git -C ~/.hermes/profiles/proxai merge --no-edit worktree-vault-approval
systemctl --user restart hermes-gateway-proxai
```

Then in a DM or on the phone: "save a note in 04-Knowledge called vault-write-test.md that says
hello". Hermes should ask; say **no** → nothing written; ask again, say **yes** → the note appears,
plus a 📝 line in #bot-audit-log. Then `/vault-writes`.
