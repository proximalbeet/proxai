# proxai-schedule: scheduled jobs from the phone, and the phone notice feed

Decided with Nathan 2026-10-23: results come to the phone as notifications; saying yes when
scheduling also approves the action, so it runs unattended at that time.

## Using it

- "delete the pomodoro app at 2am", "every morning at 8 summarize my calendar", "in 2 hours check
  the PC temps", "weekdays at 7:30am give me the news", "check my PRs every 3 hours".
  ProxAI asks "Schedule "…" for …? Say yes"; your yes creates the job.
- "what's scheduled": the jobs you made from the phone. "cancel the calendar job": removes one.
- "Remind me …" stays with the phone's own reminders (exact alarms, no PC needed).

## How it works

1. `parse_request` finds the time phrase (every day/weekday/monday at …, every N hours, in N
   minutes, at 2am / tomorrow at 9) and the action; statements and questions are left alone.
2. On yes: `cron.jobs.create_job` (Hermes's own scheduler; jobs live in the profile's `cron/`), with
   a prompt that says Nathan approved it when scheduling. Recorded in
   `~/.local/state/proxai/schedule/jobs.json`.
3. When it fires, Hermes runs it in a short-lived cron worker (task id `cron:<job>:…`):
   - proxai-apps sees an approved job and queues the build/update/delete for the gateway
     (`~/.local/state/proxai/apps-queue`), which runs it; proxai-wiki does the same for "update the wiki".
   - The answer goes to the phone: `post_llm_call` → `notices.post(...)`.
4. **Notice feed** (`notices.py`): `~/.local/state/proxai/notices/notices.jsonl`, numbered. Also used
   by the app builder ("📱 Pong is ready", "build failed") and the wiki ("📚 update waiting for your OK").
   ProxMobile reads it from the apps server (`/apps/v1/pc/notices?after=N`) when you open the app and
   about every 15 minutes in the background.

Tests: `python3 plugins/proxai-schedule/test_schedule.py` (cron store faked).
