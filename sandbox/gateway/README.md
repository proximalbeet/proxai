# Gateway folder allow-list (Phase 6: "desktop access, no browsing")

`hermes-gateway-proxai` keeps running on this desktop (Discord, voice, calendar, mode switch),
but sees an empty home folder except an allow-list. `sandbox.conf` is a systemd drop-in for the unit.

| Visible | Access | Why |
|---|---|---|
| `~/.hermes` (minus `profiles/onetouchai`, `profiles/huskytutor`) | rw | this profile + shared Hermes files (kanban.db, cron/, installs/ leases) |
| `~/.hermes/hermes-agent`, `~/.hermes/tools` | ro | Hermes code and its Python |
| `~/.local/state/proxai`, `~/.local/state/hermes`, `~/.local/share/proxai` | rw | ProxAI state/logs, gateway locks, outbox + empty-folder mounts |
| `~/.cache/huggingface`, `~/.cache/torch`, `~/.cache/nvidia` | rw | local Whisper, Kanye TTS, CUDA caches |
| `~/.local/bin` | ro | on the unit's PATH |
| `~/dev/proxai` | ro | Claude Code workspace (container writes it; the gateway reads results to send them) |
| vault | ro | |
| `05-Brain-Dump/`, `_inbox/`, `07-Security/ProxAI-Audit/` | rw | plugin captures, security log |
| `01-Personal/`, `.git/`, `.obsidian/`, rest of `07-Security/` | hidden | |
| `/run/user/<uid>/bus`, `/run/user/<uid>/systemd` | rw | mode switch (`systemd-run --user`), vault-sync "sync now" |

Everything else in `~` is hidden: `~/.ssh`, `~/.git-credentials`, `~/.config` (browser profiles,
systemd units), `~/Downloads`, `~/Documents` outside the vault (e.g. `apikeys.txt`), `~/.local/share`.
Also: `ProtectSystem=strict` (system folders read-only), `PrivateTmp=yes`, `NoNewPrivileges=yes`.

## Check it first (doesn't touch the running gateway)

    sandbox/gateway/check.sh

Runs `probe.py` and `hermes plugins list` as a throwaway systemd job under the same rules.
The probe never opens private files: hidden folders are counted, the vault is checked with
`os.access`. Expect `N/N checks passed` and the 4 ProxAI plugins listed.

## Go live

Copy (don't symlink: the gateway can write `~/.hermes`, so a symlink would let it edit its own sandbox):

    install -D -m 644 ~/.hermes/profiles/proxai/sandbox/gateway/sandbox.conf \
        ~/.config/systemd/user/hermes-gateway-proxai.service.d/sandbox.conf
    systemctl --user daemon-reload
    systemctl --user restart hermes-gateway-proxai
    systemctl --user show hermes-gateway-proxai -p ProtectHome -p ProtectSystem   # tmpfs / strict

Then watch for permission errors:
`grep -E "PermissionError|Read-only file system|No such file" ~/.hermes/profiles/proxai/logs/errors.log | tail`.

After changing `sandbox.conf`, copy it again and repeat the reload + restart.

## Roll back

    rm ~/.config/systemd/user/hermes-gateway-proxai.service.d/sandbox.conf
    systemctl --user daemon-reload && systemctl --user restart hermes-gateway-proxai

## Limits

- **Docker**: the gateway can still ask Docker to start a container with any host folder mounted.
  This allow-list stops the gateway and its host-side tools from reading files directly, but code
  running inside the gateway could get around it through Docker. Closed by the "move Hermes's Docker
  out of root" item.
- **User D-Bus**: kept for the mode switch. Anything on that bus can start a job outside the
  sandbox (`systemd-run --user`). A later fix: the gateway drops a request file that a `.path` unit acts on.
- `~/.hermes/.env` and `~/.hermes/auth.json` (shared Hermes files) stay visible.
- `hermes update` must be run from a terminal: the gateway can't write `hermes-agent`.
- `PrivateTmp`: if `skills/` ever contains a symlink, Hermes copies it to `/tmp` before mounting it into
  the container, and Docker can't see the gateway's private `/tmp` (no symlinks there today).
