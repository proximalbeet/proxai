# Obsidian Android Sync — ProxAI Vault

**Host vault path:** `/home/prox/Documents/ProxVault`
**Docker mount:** `/workspace/vault/` (ro root, `10-Wiki/` rw)
**Phone:** Android

---

## Option 1: Syncthing (Recommended)

Private, local, free. No cloud middleman.

### Host Setup
```bash
sudo apt install syncthing
systemctl --user enable syncthing
systemctl --user start syncthing
# Open web UI
xdg-open http://127.0.0.1:8384
```

In the web UI:
1. **Add Folder** → path: `/home/prox/Documents/ProxVault`
2. Note your **Device ID** (shown in Actions → Show ID)
3. After pairing Android, share the folder with it

### Android Setup
1. Install [Syncthing](https://play.google.com/store/apps/details?id=com.nutomic.syncthingandroid) from Play Store
2. Add device using host's Device ID
3. Accept shared folder → set local path (e.g. `Internal Storage/ProxAI`)
4. Install [Obsidian](https://play.google.com/store/apps/details?id=md.obsidian)
5. Open vault → select the Syncthing-synced folder

**Sync mode:** Two-way by default. Works on LAN or internet.

---

## Option 2: Remotely Save Plugin

Easiest setup if you already have Google Drive, OneDrive, Dropbox, or S3.

### Setup (Desktop)
1. Obsidian → Settings → Community Plugins → Browse → search "Remotely Save"
2. Install and enable
3. Configure with your cloud provider (OAuth for Google/OneDrive, keys for S3)
4. Run a manual sync to verify

### Setup (Android)
1. Install Obsidian for Android
2. Install Remotely Save plugin (same steps)
3. Connect same cloud account
4. Sync — vault appears

**Note:** Vault is read-only offline until next sync.

---

## Comparison

| | Syncthing | Remotely Save |
|--|-----------|---------------|
| Privacy | ✅ Fully local | ⚠️ Via cloud |
| Speed | ✅ Fast (LAN) | 🟡 Network dependent |
| Setup effort | ~10 min | ~5 min |
| Works offline | ✅ Last sync state | 🟡 Read-only |
| Cost | Free | Free (your cloud) |

---

## Notes
- Obsidian Sync (paid, $4-8/mo) is the official option — end-to-end encrypted, built-in
- iCloud works for iOS but not Android
- Git-based sync (Obsidian Git plugin) also works but requires comfort with git on mobile
