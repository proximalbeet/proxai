# ProxAI calendar (Phase 9)

Hermes adds and changes events on **its own "ProxAI" Google calendar** and nothing else. The
calendar lives in your Google account, so it shows up under "My calendars" next to yours.

## Permissions

| | |
|---|---|
| Google scopes | `calendar.app.created` (calendars this app created + their events: the **only place it can write**), `calendar.events.readonly` (**read** your own events; added 10-18 by Nathan, `--no-read-events` leaves it out) and `calendar.freebusy` (busy times). No Gmail, Drive or Docs |
| Your events | Read-only, `primary` calendar only, and only title, time and place: descriptions and attendee lists are left out, and the tool marks titles as untrusted text (anyone can send you an invite). They carry no event_id, so update/delete can't target them. Recommended: Google Calendar → Settings → Event settings → "Add invitations to my calendar" → **Only if the sender is known** |
| Being found | Plugin tools sit behind Hermes's Tool Search, so a `pre_llm_call` hook adds a one-line pointer to the `calendar` tool when a message is about the calendar |
| Broader token | Refused. A token with any other scope stops the tool ("more access than ProxAI is allowed") |
| Which calendar | Fixed by `calendar.json` at setup. The model has no calendar field to change |
| Invites | Never: no attendees, every write sends `sendUpdates=none` |
| Deletes | The model can only *ask*: it gets a code, and only you typing `/calendar-confirm <code>` deletes (30 min, one use) |
| Runaway loop | 20 changes per hour max |
| Where it runs | Host side, in the gateway (like the web tools). The Docker sandbox stays offline |
| Logging | Every create/update/delete → `~/.local/state/proxai/calendar.log` + one 📅 line in #bot-audit-log |
| Token | `~/.hermes/profiles/proxai/google-calendar/` (folder 700, files 600, not in git, not mounted in the container). The tool refuses to run if `token.json` is readable by others |

Caveat: all Hermes profiles run as the same Linux user, so another profile's gateway *could* read
the file. Separating them needs the gateway sandboxing from Phase 6 ("desktop access, no browsing").

**Tool:** `calendar` with `agenda` (your events + ProxAI's) / `list` (ProxAI only) / `busy` /
`create` / `update` / `delete`.
**Commands:** `/calendar` (next 7 days), `/calendar-confirm <code>`.

**Adding the read scope to an existing setup:** Google Cloud → Data access → add
`.../auth/calendar.events.readonly` → Save; then run `setup_calendar.py` again (no arguments; it
keeps the client and the ProxAI calendar) and `--selftest`. No gateway restart needed: the plugin
picks up the new login by itself. Run the new code (merge) **before** signing in again: the older
plugin refuses a login with the extra scope. In Testing mode, the same command is the weekly re-login.

## Go live (Nathan)

1. **Google Cloud** (console.cloud.google.com, signed in as the Google account whose calendar you use):
   1. New project "ProxAI"
   2. APIs & Services → Library → **Google Calendar API** → Enable
   3. OAuth consent screen (Google Auth Platform): External, app name "ProxAI", your email.
      Data access → add only `.../auth/calendar.app.created` and `.../auth/calendar.freebusy`
   4. Audience → **Publish app** (In production). In "Testing" mode Google expires the login
      after **7 days**. Unverified is fine for your own account: at sign-in click
      *Advanced → Go to ProxAI (unsafe)*
   5. Clients → Create client → **Desktop app** → download the JSON
2. Merge (after the Phase 6/7 merges): `git -C ~/.hermes/profiles/proxai merge worktree-calendar`
3. Sign in and create the calendar (opens the browser):
   ```fish
   python3 ~/.hermes/profiles/proxai/plugins/proxai-calendar/setup_calendar.py --client-secret ~/Downloads/client_secret_*.json
   rm ~/Downloads/client_secret_*.json
   ```
   Add `--no-freebusy` if Hermes shouldn't see when you're busy
4. Permission test: `python3 ~/.hermes/profiles/proxai/plugins/proxai-calendar/setup_calendar.py --selftest`
   → create / move / list / delete PASS, **write to main calendar refused**, **read main calendar refused**
5. `systemctl --user restart hermes-gateway-proxai`. The branch already enables the plugin,
   adds `proxai_calendar` to `platform_toolsets` (discord, cli), sets the channel limits and
   disables `google-workspace`. Steps 3–4 can run from the branch folder before the merge
   (`~/.hermes/profiles/proxai/.claude/worktrees/calendar/plugins/proxai-calendar/`); the login is
   saved in the profile either way. Merged before setup, the tool just answers "not set up yet"
6. From Discord: "put a 30-minute study block on my calendar tomorrow at 3" → event in the ProxAI
   calendar, 📅 line in #bot-audit-log; "move it to 4" → moved; "delete it" → a code, nothing
   deleted until `/calendar-confirm <code>`

## Decisions (Nathan, 2026-10-18)

1. **Channels that can schedule: DMs, #general-chat, #daily-goals.** In this branch's
   `routing.json`: `calendar` added to the `chat` profile (#general-chat; also the only channel on
   it), and #daily-goals moved to a new `capture_calendar` profile (capture tools + `calendar`) so
   #brain-dump and #focus-sessions stay without it. Both channel prompts say when to use it. DMs
   have no channel limits. Checked with the guard: allowed in DMs/#general-chat/#daily-goals,
   blocked elsewhere; 35 discord-routing tests pass. Note: #daily-goals runs on qwen (tool calls
   1/3 in Phase 3), so watch the first few; the plugin still saves the goal itself either way
2. **Busy times** (`calendar.freebusy`): on by default; `--no-freebusy` at setup turns it off
3. **`google-workspace` skill disabled in proxai** (`skills.disabled` in this branch's
   `config.yaml`). It asked for full Calendar, Gmail (read/send), Drive, Docs and Sheets

## Tests

`python3 plugins/proxai-calendar/test_calendar.py`: 11 tests against a fake Google API: create /
list / move (length kept), all-day default, fixed calendar (a `calendar_id` argument is ignored),
main-calendar write refused, delete only with your code (wrong code / reuse refused), broader token
refused, token file must be 600, busy shows times only and is off without the scope, 20/hour limit,
bad input (empty title, bad date, end before start, > 7 days, long title, bad event id, > 92-day range).
