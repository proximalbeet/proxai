#!/usr/bin/env bash
# Phase 10 go-live helper. User-level only (no sudo). Safe to run again.
#
#   bash install.sh          put `proxai-mode` on your PATH (do this first, then test the switch)
#   bash install.sh --boot   AFTER the switch is confirmed working: start up in the last mode
#                            after every restart (server stays server, personal stays personal)
#
# Undo: rm ~/.local/bin/proxai-mode; systemctl --user disable proxai-mode-reapply.service
set -euo pipefail
MODE_DIR="$HOME/.hermes/profiles/proxai/mode"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$HOME/.local/bin" "$UNIT_DIR" "$HOME/.local/state/proxai"
chmod +x "$MODE_DIR/proxai_mode.py"
ln -sf "$MODE_DIR/proxai_mode.py" "$HOME/.local/bin/proxai-mode"

if [[ "${1:-}" == "--boot" ]]; then
    ln -sf "$MODE_DIR/proxai-mode-reapply.service" "$UNIT_DIR/proxai-mode-reapply.service"
    systemctl --user daemon-reload
    systemctl --user enable proxai-mode-reapply.service
    echo "Restart-in-last-mode is on. Current mode: $("$MODE_DIR/proxai_mode.py" status | head -1)"
    echo "Test: reboot, then 'proxai-mode status' shows 'reapplied' and the lights/sleep block match the mode."
else
    echo "Installed. Try:  proxai-mode status   proxai-mode check server   proxai-mode set personal --dry-run"
    echo "Once the switch works: bash $MODE_DIR/install.sh --boot   (start in the last mode after restarts)"
fi
command -v openrgb >/dev/null || echo "Note: OpenRGB isn't installed; RGB steps are skipped until: sudo pacman -S openrgb"
