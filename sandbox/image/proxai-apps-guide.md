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
- `features`: only what the app uses, from: `vibrate`, `notifications`, `speech`, `ask`, `pc-stats`.

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
  - `await ProxAI.pcStats()` → feature `pc-stats`: Nathan's PC (StormTrooper: RTX 5070 Ti, i9-13900K),
    read through ProxMobile. Rejects with a message if ProxMobile can't reach the PC; show it and
    retry every few seconds. Poll every 2-5 s while visible (`document.visibilityState`). Returns:
    ```
    {"watching": true, "age_s": 1,              // false/old = the PC's watcher is off or unreachable
     "latest": {"time": "2026-10-22T22:41:07", "gpu_c": 41, "gpu_w": 25.3, "gpu_use": 0,
                "vram_mib": 15724, "vram_total_mib": 16303, "fan_pct": 0, "sm_mhz": 1600,
                "throttle": "0x0", "cpu_pkg_c": 55, "cpu_core_max_c": 60, "cpu_use": 3.5,
                "ram_used_mb": 14000, "ram_avail_mb": 16800, "swap_mb": 5000,
                "limits": {"gpu_warn_c": 75, "gpu_crit_c": 82, "gpu_power_warn_w": 250,
                           "cpu_warn_c": 85, "cpu_crit_c": 95, "ram_warn_mb": 2048}},
     "history": [{"minute": "21:42", "gpu_c": 41, "gpu_w": 43, "gpu_use": 0, "cpu_c": 60,
                  "cpu_use": 9, "ram_free_mb": 16600}, ...],   // last 60 minutes, peaks per minute
     "alerts": [{"id": 12, "time": "...", "level": "ALERT" | "CRIT", "kind": "gpu_t", "msg": "..."}]}
    ```
    Any value can be null. Colour against the limits (warn = amber, crit = red). The PC watches and
    protects itself 24/7 and ProxMobile already notifies about alerts; the app is a live view.
    Draw charts with inline SVG or canvas (no chart libraries: no network).
  - `ProxAI.app` → `{id, name, version, features}`
- Keep it one screen where possible, clear labels, works one-handed. No tracking, no ads.

## Steps (report each with --status so Nathan sees progress on the phone)

Ship it the way a careful developer would: nothing reaches the phone until it has been used in a
real browser at phone size and looked at.

```bash
D=/workspace/proxai/apps/<id>
proxai-app-build --status planning "Planning the app" $D
```
1. **Plan.** Write `PLAN.md`: every feature the request asks for or clearly implies (one line
   each), the screens, the data kept in `localStorage`, and the edge cases (empty state, very long
   text, rotating the phone, the app reopened mid-task). For an update, also list what must keep
   working.
2. **Build** (`--status writing "Writing the screens"`). Logic in plain functions (e.g.
   `www/logic.js`) that also load in Node; UI code separate.
3. **Test the logic** (`--status testing "Testing"`): `node --check` every JS file, and a Node test
   (`node --test` or `assert`) for the logic, including the edge cases from the plan.
4. **Test it in the browser**: write `ui-test.js` that drives the real page through EVERY feature
   in `PLAN.md` and asserts what the screen shows. Example:
   ```js
   module.exports = async ({ page, clock, assert, calls }) => {
     await page.getByRole('button', { name: 'Start' }).tap();
     await clock.runFor(25 * 60 * 1000);                   // fake time: 25 minutes pass instantly
     assert.match(await page.locator('#phase').innerText(), /break/i);
     assert.ok((await calls()).some(c => c.name === 'vibrate'));   // ProxAI calls the app made
     await page.reload();                                   // data must survive a restart
   };
   ```
   Then run `proxai-app-check $D --script ui-test.js` (add `--fast-forward SECONDS` for timers).
   It also taps every control, checks dark and light mode, sideways scrolling, tap-target size, cut
   off text, JS/console errors, network use and features used without being declared.
5. **Look at it.** Open every screenshot the check prints (`.check/*.png`) with the Read tool. Is
   everything readable in both themes, aligned, nothing overlapping or cut off, does it look like a
   finished app? Fix what isn't, then go back to step 3. Repeat until the check reports no problems
   AND the screenshots look right.
6. **Build**: `proxai-app-build $D`. It refuses files that haven't passed the check (any edit after
   the check means checking again). Fix and repeat until it prints `"ok": true`.

`proxai-app-build --rollback <code> $D` restores an older build as a new one (no new check needed).
