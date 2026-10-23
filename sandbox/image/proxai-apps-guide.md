# How to build a ProxAI app (for Claude Code)

A ProxAI app is a small Android app for Nathan's phone. You write it as web files (HTML, CSS,
JavaScript); `proxai-app-build` packs them into the ProxAI shell and signs the APK. The phone
installs it from ProxMobile's Apps tab. Example: `/opt/proxai-shell/example/proxai-test/`.

## Folder: /workspace/proxai/apps/<id>/

```
proxai-app.json   {"id": "<id>", "name": "Short Name", "version": "1.0",
                   "description": "One sentence: what it does.",
                   "features": [], "color": "#4DA8FF"}
www/index.html    entry page; other files (www/app.js, www/style.css, images) load by relative path
icon.xml          optional: Android <vector>, 108x108 viewport, white shapes, keep inside the
                  middle 66x66 (the rest gets cropped); no references to other resources
```

- `id` is given to you; never change it. `name` max 40 chars. Bump `version` (1.0 → 1.1) on every
  change. `color` is the icon's accent.
- `features`: only what the app uses, from: `vibrate`, `notifications`, `speech`, `ask`.

## Rules

- **No network.** The app has no internet permission: no CDNs, web fonts, remote images or
  `fetch` to the internet. Everything is inline or a local file under `www/`.
- **Mobile first:** `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">`,
  44px+ touch targets, no horizontal scroll, `env(safe-area-inset-bottom)` padding.
- **ProxAI look:** dark background `#0E0F13`, text `#E8EEFC`, muted `#9AA7C2`, cards `#161A24`,
  accents a blue gradient `#0B3D91 → #4DA8FF` with a soft glow, cyan highlight `#38E1FF`, rounded
  corners (14px). The shell already shows the app name in a title bar, so don't repeat a big header.
- **Data:** `localStorage` (kept per app, survives restarts). Export/import as JSON text if the
  data matters.
- **Phone features** (check `window.ProxAI` exists, so the page also works in a browser):
  - `ProxAI.toast(text)` always
  - `ProxAI.vibrate(ms)` → feature `vibrate`
  - `ProxAI.notify(title, text)`, `ProxAI.remind(date, text)` → feature `notifications`
    (returns false until Nathan allows notifications; say so in the UI)
  - `ProxAI.speak(text)` → feature `speech`
  - `await ProxAI.ask(prompt)` → feature `ask` (not connected yet: catch the error and degrade)
  - `ProxAI.app` → `{id, name, version, features}`
- Keep it one screen where possible, clear labels, works one-handed. No tracking, no ads.

## Steps (report each with --status so Nathan sees progress on the phone)

```bash
D=/workspace/proxai/apps/<id>
proxai-app-build --status planning "Planning the app" $D
proxai-app-build --status writing "Writing the screens" $D
proxai-app-build --status testing "Testing" $D
proxai-app-build $D          # prints {"ok": true, ...} or {"ok": false, "error": ...}
```

Testing: put the logic in plain functions (e.g. `www/app.js`) and test them with Node
(`node --test` or a small script with `assert`); run `node --check` on every JS file; make sure
`proxai-app-build --check $D` passes. Fix and rebuild until the build prints `"ok": true`.
`proxai-app-build --rollback <code> $D` restores an older build as a new one.
