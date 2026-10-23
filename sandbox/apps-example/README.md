# ProxAI app example

`proxai-test/` is the reference app for the app generator (checklist Phase 15): the folder
format Claude Code writes (`proxai-app.json` + `www/`), using every phone feature.

Build it inside the sandbox image: `python3 sandbox/test_app_build.py` (or, in the container,
`proxai-app-build /workspace/proxai/apps/<app>`). How the whole generator works, tech stack and
changelog: ProxMobile `docs/PROXAI-APPS.md`.
