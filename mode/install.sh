#!/usr/bin/env bash
# Phase 10 go-live helper: puts `proxai-mode` on your PATH and enables the re-apply-at-boot service.
# User-level only (no sudo). Safe to run again. Undo: rm ~/.local/bin/proxai-mode;
# systemctl --user disable --now proxai-mode-reapply.service
set -euo pipefail
MODE_DIR="$HOME/.hermes/profiles/proxai/mode"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$HOME/.local/bin" "$UNIT_DIR" "$HOME/.local/state/proxai"
ln -sf "$MODE_DIR/proxai_mode.py" "$HOME/.local/bin/proxai-mode"
chmod +x "$MODE_DIR/proxai_mode.py"
ln -sf "$MODE_DIR/proxai-mode-reapply.service" "$UNIT_DIR/proxai-mode-reapply.service"
systemctl --user daemon-reload
systemctl --user enable proxai-mode-reapply.service

echo "Installed. Try:  proxai-mode status   proxai-mode check server   proxai-mode set personal --dry-run"
command -v openrgb >/dev/null || echo "Note: OpenRGB isn't installed; RGB steps are skipped until: sudo pacman -S openrgb"
