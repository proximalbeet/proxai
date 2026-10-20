# Server / personal mode (checklist Phase 10)

One switch for StormTrooper:

| | SERVER (you're away) | PERSONAL (you're at the desk) |
|---|---|---|
| Busy check | **Refuses** if a game/render is running (GPU use or power, CPU, a big VRAM user, Steam/Proton/OBS...) | none |
| Sleep | blocked while the mode is on | normal |
| Screen | locked, display off | display on |
| RGB | off (OpenRGB) | your `personal` OpenRGB profile |
| Verity Minecraft voice | stopped (only runs when the mod is on) | left alone |
| Local model (tier1 channels) | kept loaded; bigger model (see below) | unloads 2 min after use |
| Kanye voice | on and kept loaded; Discord replies use the warm server (~1 s/sentence, no Ollama unload) | **off**: Hermes is text-only, the phone uses its own voice |

Everything is user-level (no sudo). What each mode does is in [`modes.toml`](modes.toml): add, remove or
reorder steps there and the next switch uses it.

## Using it

```
proxai-mode status                    # current mode
proxai-mode check server              # would server mode be allowed right now? (readings)
proxai-mode set server                # switch (refuses if busy; --force to override)
proxai-mode set personal --dry-run    # show what it would run, change nothing
proxai-mode modes                     # list modes and their steps
```

**From Hermes** (Discord DMs, the phone app, or `hermes` CLI only, never channels):
"switch to server mode" → Hermes checks the PC isn't busy and asks *"Switch to SERVER mode? That
will: … Say yes to confirm."* → your **next** message (typed, phone voice, or a Discord voice
note) must be a plain yes. Anything else cancels. The yes is checked by the plugin, not the model,
so Hermes can't approve its own request. Requests expire after 3 minutes. `/mode` shows the mode.
Every switch (and refusal) goes to `~/.local/state/proxai/mode.log` and #bot-audit-log.

## How it fits together

- `proxai_mode.py`: the switch. Writes `~/.local/state/proxai/mode.json` (mode, when, who, step
  results, and a `hermes` section). One switch at a time (lock file). A failing step doesn't stop the
  others; `status` lists what failed.
- `plugins/proxai-mode/`: the `proxai_mode` tool + confirm hook. Runs the switch with
  `systemd-run --user` so Ollama/voice restarts never touch the gateway.
- Hermes follows the mode without a restart: `discord-routing` reads `hermes.tier1` for the tier1
  model; `voice/tts_pipeline.py` reads `hermes.voice` / `hermes.voice_warm`.
- Ollama and voice-server settings are systemd drop-ins named `50-proxai-mode.conf` under
  `~/.config/systemd/user/{ollama,proxai-phone-voice}.service.d/`.
- `proxai-openrgb.service` keeps a headless OpenRGB server running. The Corsair Commander CORE
  only holds a colour while OpenRGB runs, so the rgb step asks this server (over its SDK port,
  6742) to load a saved profile. The `openrgb` CLI isn't used: in 1.1 it quits before the server
  sends its device list. The OpenRGB window connects to the server too.
- `proxai-mode-reapply.service` puts the mode back after a reboot (lights, sleep block, Ollama);
  it doesn't lock the screen.

## Go-live (Nathan)

1. `git -C ~/.hermes/profiles/proxai merge worktree-phase10-modes`
2. `bash ~/.hermes/profiles/proxai/mode/install.sh` (puts `proxai-mode` on your PATH and starts the
   OpenRGB server, `proxai-openrgb.service`)
3. RGB (OpenRGB 1.1; sees both RAM sticks, HyperX keyboard, Lighting Node Pro, Commander Core, ASUS
   board, Razer mouse; the OMEN monitor light isn't detected). Save two profiles, ticking every device
   in the Profile Editor (an empty profile does nothing): **`default`** (personal mode) = your everyday
   look; **`Off`** (server mode) = every device in Direct, all black (the RAM has no "Off" mode, so
   black is how it goes dark). Profiles saved from the window show up right away; a profile file copied
   in by hand needs `systemctl --user restart proxai-openrgb`.
4. `systemctl --user restart hermes-gateway-proxai`
5. Try: `proxai-mode check server`, `proxai-mode set personal`, then from a DM "switch to server mode".
6. **Once both modes work:** `bash ~/.hermes/profiles/proxai/mode/install.sh --boot`. After every
   restart the PC comes back in the mode it was in (lights, sleep block, Ollama model, voice), about
   20 s after boot, even before you log in. The screen lock and display-off steps don't re-run (the
   login screen covers that).

## The server-mode model: `qwen3.5:35b-a3b` (picked 2026-10-19)

Mixture-of-experts, 35B total / 3B active, Q4_K_M, 22 GB download. Measured 2026-10-20 at 64K context
with ~3.3 GB of VRAM already taken (about what the warm voice holds in server mode):

| | qwen3.5:9b (personal) | qwen3.5:35b-a3b (server) |
|---|---|---|
| Placement | 100% GPU, ~7 GB | 97% GPU / 3% CPU, ~11.8 GB VRAM |
| Load time | ~2 s | ~16 s (once; kept loaded in server mode) |
| Writing | ~104 tok/s | ~57 tok/s |
| Reading prompts | ~5,000 tok/s | ~580 tok/s (a 15.8k-token prompt: 27 s) |
| Quick tool check (3 cases) | 3/3 | 3/3 |

The quick check was too easy to separate them; rerun the Phase 3 tool tests in server mode after
go-live. Expect the first reply of a fresh tier1 conversation to take ~10-20 s (prompt reading);
follow-ups reuse Ollama's prompt cache.

Server mode sets `OLLAMA_GPU_OVERHEAD` to 4 GB (`gpu_overhead_mib` in `modes.toml`) so the model
always leaves room for the Kanye voice; without it Ollama loads first and fills the card.

To change the model later: `ollama pull <model>`, set `[server.hermes].tier1.model` and the ollama
step's `preload` in `modes.toml`, and add it under `custom_providers` → `models` in `config.yaml`
with `context_length: 65536` (Hermes needs ≥64K).

## Tests

```
python3 mode/test_proxai_mode.py                                  # engine (fake commands, temp state)
~/.hermes/hermes-agent/venv/bin/python plugins/proxai-mode/test_mode_plugin.py
voice/.venv-chatterbox/bin/python voice/test_tts_mode.py          # fake voice server
(cd plugins/discord-routing && ~/.hermes/hermes-agent/venv/bin/python test_mode_routing.py)
```
