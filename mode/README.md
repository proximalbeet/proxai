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
- `proxai-mode-reapply.service` puts the mode back after a reboot (lights, sleep block, Ollama);
  it doesn't lock the screen.

## Go-live (Nathan)

1. `git -C ~/.hermes/profiles/proxai merge worktree-phase10-modes`
2. `bash ~/.hermes/profiles/proxai/mode/install.sh` (PATH link + reapply service)
3. RGB: `sudo pacman -S openrgb` (installs its udev rules; replug or reboot once), open OpenRGB,
   check it sees the Aura board, Corsair Lighting Node Pro + Commander CORE, Razer mouse, HyperX
   keyboard. Set your colors and **Profile → Save as `personal`**. Optional: save `off` too
   (otherwise "off" = all lights black). The OMEN monitor light probably isn't supported.
4. `systemctl --user restart hermes-gateway-proxai`
5. Try: `proxai-mode check server`, `proxai-mode set personal`, then from a DM "switch to server mode".

## Open decision: the server-mode model

`modes.toml` uses `qwen3.5:9b` as a placeholder. The 5070 Ti has 16 GB; in server mode the warm
voice model takes ~3.4 GB and the desktop ~0.5 GB, so ~12 GB is left. Options (not benchmarked yet):

| Model | Size | Fits next to the voice? | Notes |
|---|---|---|---|
| `qwen3.5:9b-q8_0` | 10 GB | yes | same model, higher precision. Small step up, safest |
| `qwen3.5:35b-a3b` | 22 GB | no, splits ~11 GB GPU / ~11 GB RAM | much more capable. Mixture-of-experts (3B active) so a split should stay usable; test the speed. Uses ~11 GB of your 31 GB RAM |
| `gpt-oss:20b` | ~14 GB | no (voice would push it out) | |
| `qwen3.5:27b` | 17 GB | no | dense, too slow when split. Not recommended |

To switch: `ollama pull <model>`, then in `modes.toml` set `[server.hermes].tier1.model` and the
ollama step's `preload` to it, and add it under `custom_providers` → `models` in `config.yaml` with
`context_length: 65536` (Hermes needs ≥64K). Then rerun the Phase 3 tool tests in server mode.

## Tests

```
python3 mode/test_proxai_mode.py                                  # engine (fake commands, temp state)
~/.hermes/hermes-agent/venv/bin/python plugins/proxai-mode/test_mode_plugin.py
voice/.venv-chatterbox/bin/python voice/test_tts_mode.py          # fake voice server
(cd plugins/discord-routing && ~/.hermes/hermes-agent/venv/bin/python test_mode_routing.py)
```
