# ProxAI

ProxAI is a personal AI assistant that lives in Discord. It's a profile for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It uses an Obsidian vault as long-term memory and picks a model for each channel. Every file and command it touches stays inside a locked-down Docker container.

This repo holds the agent's definition: its persona, config, routing plugin, voice scripts and custom skills. Keys, logins, memory and sessions are not tracked.

## How it works

```
Discord ──► Gateway (host, systemd) ──► discord-routing plugin ──► model tier
                                                                     │
                     Obsidian vault ◄── tools (Docker sandbox) ◄─────┘
```

1. You message a channel, a thread or a DM.
2. The gateway runs on the host as `hermes-gateway-proxai.service`.
3. The `discord-routing` plugin picks a model tier and that channel's instructions.
4. The model reads, decides and replies. File and command tools run inside a Docker container, and only some vault folders are mounted in it.

## Model tiers

| Tier | Model | Runs on | Channels |
|---|---|---|---|
| tier1 | `qwen3.5:9b` (Ollama, 64K context) | Local GPU | brain-dump, focus-sessions, daily-goals, ollama-outputs, model-logs, bot-commands, build-status, bot-audit-log, announcements |
| tier2 | Claude Sonnet 5.5 | Anthropic API | general-chat, vault-queries, research-drops, arduino-logs, build-errors, quest-dev-logs, vr-ideas, DMs |
| tier3 | Claude Opus 5.5 | Anthropic API | code-assist |

- **Routing and instructions:** routing is set by channel name in `plugins/discord-routing/routing.json`. Each channel also has its own instructions there: its purpose, where it may write, and what to post back.
- **Threads:** a thread inherits its parent channel's tier and instructions.
- **Fallbacks:** unlisted channels fall back to tier1. Starting a message with `!opus` sends just that message to tier3.
- **Reloading:** the plugin reloads `routing.json` when the file changes, so you don't need to restart.

## Vault access

The vault is `~/Documents/ProxVault`. It's mounted into the container at `/workspace/vault`.

| Folder | Access |
|---|---|
| Vault root (all folders) | Read-only |
| `_inbox/` | Read and write |
| `10-Wiki/` | Read and write |
| `05-Brain-Dump/` | Read and write (#brain-dump saves ideas here directly) |
| `01-Personal/`, `07-Security/` | Meant to be hidden from the container. **Not done yet:** still readable (fix in Phase 6) |

Notes carry `type` and `area` frontmatter. The SOUL.md filing rules map each type to a folder. Some examples:

| Type | Folder |
|---|---|
| `note` | `04-Knowledge` (with area `knowledge`) |
| `task` | `05-Brain-Dump` |
| `project` | `02-Projects` |
| `focus-log` | `06-Focus-Logs` |
| `daily` | `00-Daily` |

A host-side **refiler** will move notes from `_inbox/` into the read-only folders. It isn't built yet; that's Phase 6.

## Security layers

| Layer | What it does | Status |
|---|---|---|
| Discord | Only one allowed user; channels need an @mention (except #brain-dump) | Live |
| Tools | `computer_use` off on discord/cli/cron; approvals manual; cron can't run commands | Live |
| Container | Non-root (1000:1000), all capabilities dropped, no-new-privileges, pids/memory caps, no internet | Live |
| Vault mounts | Root read-only; only `_inbox/`, `10-Wiki/`, `05-Brain-Dump/` writable | Live |
| Private folders | Hide `01-Personal/` and `07-Security/` from the container | Phase 6 |
| Host | Folder allow-list for the gateway, rootless Docker | Phase 6 |
| Per-channel tools | Enforce each channel's tool limits in config, not just in its instructions | Phase 6 |
| Secrets | `.env` is `600` and never committed (see `.gitignore`) | Live |

## Voice

The `kanye` TTS provider uses Chatterbox voice cloning (`voice/tts_pipeline.py`). Before loading, it unloads Ollama models so the GPU doesn't run out of memory. Voices without a reference clip use RVC (`voice/tts_rvc.py`). Model files, reference audio and virtualenvs aren't tracked.

## Repo layout

| Path | What it is |
|---|---|
| `SOUL.md` | Persona (Ye-inspired) and the vault filing rules |
| `SOUL.gru.md` | Previous persona, kept for reference |
| `config.yaml` | Hermes profile config: models, Docker backend, mounts, Discord, TTS, approvals |
| `profile.yaml` | Profile description |
| `plugins/discord-routing/` | Channel → tier routing, per-channel instructions, `!opus` override |
| `voice/` | TTS scripts only |
| `skills/knowledge/vault-manager/` | Vault search, filing and dashboards |
| `skills/openclaw-imports/` | Wiki skills: ingest, query, lint, dedup, cross-link, research and more |

Not tracked: `.env`, `auth.json`, memory, sessions, logs, caches, Hermes's bundled skills, voice models.

## Running it

```fish
# Install and start the gateway as a user service
hermes -p proxai gateway install
hermes -p proxai gateway start

# After changing config.yaml (routing.json reloads by itself)
hermes -p proxai gateway restart

# Logs
journalctl --user -u hermes-gateway-proxai -f
```

**Requirements:**
- Ollama runs with `OLLAMA_CONTEXT_LENGTH=65536`. Hermes needs at least 64K context.
- `qwen3.5:9b` is pulled.
- Docker is running.

**`.env`** (in the profile folder, not committed) needs these variables:
- `DISCORD_BOT_TOKEN`
- `DISCORD_ALLOWED_USERS`
- `DISCORD_HOME_CHANNEL`
- `ANTHROPIC_API_KEY`

## Status and roadmap

The setup is being verified phase by phase against a checklist.

| Phase | Topic | Status |
|---|---|---|
| 0–3 | Baseline, security, routing, tool calling | Verified |
| 4 | Discord ↔ vault (channel instructions live) | Verified. tier1 qwen doesn't follow channel rules; fix in Phase 6 |
| 5 | Persona and voice | Verified; SOUL.md cleanup waiting to merge |
| 6 | Fixes: refiler, hide private folders, host allow-list, rootless Docker, per-channel tool limits, plugin-written captures, cost controls | Next |
| 7 | Claude Code runs every command inside the container | Planned |
| 8 | Vault git auto-sync without merge conflicts | Planned |
| 9 | Google Calendar on its own calendar, narrow permission | Planned |
| 10 | Server vs user mode command | Planned |
| 11 | Computer use, only behind a sandbox and per-action approval | Optional |
| 12 | `#learn` tutor and course builder (moves to the HuskyTutor profile) | Future |
| 13 | Quest 3S VR dev | Last |

Known gaps:
- **qwen and tools:** qwen3.5:9b is unreliable at tool calls (1 of 3 in testing). Tool work is moving to plugin-written captures and Claude Code.
- **Privacy block:** the cloud-tier block on private folders is prompt-only until Phase 6.
