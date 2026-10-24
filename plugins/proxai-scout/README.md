# proxai-scout: overnight gigs and app ideas

Every night ProxAI reads public job boards and forums, Claude Code picks the paid gigs and app ideas
that fit Nathan, and one notification lands on the phone in the morning. It only reads and suggests:
nothing is ever sent, applied for or bought for him.

Decided with Nathan 2026-10-23: gigs (small paid jobs: AI agents, automation, Python, Android/web
apps) and app ideas (pain points people post, with extra weight on construction and trades, his
target market). Hermes, not n8n. No hours limit (his schedule changes week to week): every gig is
tagged small / medium / large instead.

## Using it (phone, DMs or CLI)

| Say | What happens |
|---|---|
| "what did the scout find" / "show the gigs" / "show app ideas" | the latest picks, numbered |
| "pitch gig 2" | the draft reply for gig 2 plus its link; you send it yourself |
| "tell me about idea 1" | the full idea and the post it came from |
| "build idea 1" | proxai-apps asks "Build a new app ...? Say yes" first |
| "not interested in gig 3" / "more like gig 1" | goes into the feedback the next run reads |
| "only small gigs" / "show gigs of any size" | hides medium/large gigs for 7 days / undoes it |
| "run the scout" / "scout status" | run now (a few minutes) / last run |

Nightly at 05:00 (`sources.json` `nightly_at`). A quiet night sends no notification; a failed run
sends one ("Scout run failed").

Your preferences: `09-Career/scout-profile.md` in the vault (edit in Obsidian; `wiki: no` keeps it out
of the wiki). Without it the plugin's `profile.md` is used. Feedback:
`~/.local/state/proxai/scout/feedback.md`.

## Gig board (2026-10-24)

Every pick also lands on a board (`~/.local/state/proxai/scout/tracker.json`, `tracker.py`) with a status,
notes, an hours estimate and an optional calendar block:

- Gigs: new → interested → pitched → won → done (or passed). Ideas: new → interested → building → built (or passed).
- Chat: "more like gig 1" = interested, "not interested in gig 2" = passed, "I pitched gig 3", "I got gig 3",
  "finished gig 3".
- Apps: a ProxAI-built app with the `scout` feature reads the board (`ProxAI.scout()`) and sends changes
  (`ProxAI.scoutUpdate({id, status, notes, hours, block})`) through ProxMobile 0.6.4+ and the apps server
  (`GET /v1/scout`, `POST /v1/scout/update`). The apps server only drops each change in `scout/inbox/`; this
  plugin checks it again and applies it within a minute, so it stays the only writer.
- `block: {start, hours}` puts a block on the ProxAI calendar through proxai-calendar (its 20-writes-an-hour
  limit, log and audit line) and sends a "📅 Blocked …" notice. The app asks before sending one.
- Passed/done items older than 90 days are dropped; at most 1000 items.

## How it works

1. **Read the feeds** (Python, in the gateway): Reddit RSS, one combined feed per group of
   subreddits, 90 s apart (Reddit blocks faster readers; a block waits 2 minutes and retries), the
   Hacker News "Who is hiring?" thread (contract / freelance / part-time / intern posts), RemoteOK.
   Keyword filters per feed, posts from the last 3 days, nothing already seen (60 days).
2. **Pick** (Claude Code in the sandbox, Sonnet; Opus if that fails): the batch, profile and
   feedback are written to `~/dev/proxai/scout/runs/<run>/`; Claude Code reads them plus the resume
   (`09-Career/resume/resume-current.md`) and `10-Wiki/index.md`, and writes `result.json`
   (fit/score, pay, size, why, a draft pitch, an app idea).
3. **Check**: only ids from the batch count; titles and links come from the feeds, never from
   Claude's answer. Top 5 gigs and 3 ideas are kept (`~/.local/state/proxai/scout/latest.json`, a
   readable copy in `digests/`), and the notice goes out.

## Safety

The posts are written by strangers, so a post could try to give Claude Code orders. That run gets
only file tools (`cc-run --tools Read,Write,Glob,Grep`: no shell, no web) and, like every sandbox
run, no MCP servers or claude.ai connectors (`--strict-mcp-config`). The sandbox only reaches
Anthropic, npm, PyPI and GitHub, and the vault is read-only there. If the sandbox image predates
`--tools`, the run fails (it is never retried without the limit): rebuild the image.

## Tech stack

Python standard library (urllib, ElementTree, html.parser), Claude Code CLI through the
claude-code-delegation runner (`cc-run`), the proxai-schedule notice feed, proxai-apps for builds.

## Tests

`python3 plugins/proxai-scout/test_scout.py` (feeds and Claude Code faked; the sandbox shell runs
locally).

## Changelog

- 0.1.0 (2026-10-23): first version.
- 0.2.0 (2026-10-24): gig board (statuses, notes, hours, calendar blocks) for chat and ProxAI apps.
