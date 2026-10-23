"""Tests for proxai-schedule (Hermes's cron store is faked; nothing is really scheduled).

Run: python3 plugins/proxai-schedule/test_schedule.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="schedule-test-", dir=os.environ.get("TMPDIR")))
os.environ["PROXAI_STATE_DIR"] = str(TMP)
spec = importlib.util.spec_from_file_location("proxai_schedule_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_schedule_plugin"] = plugin
spec.loader.exec_module(plugin)

NOW = dt.datetime(2026, 10, 23, 14, 30)


class FakeCron:
    def __init__(self):
        self.jobs = {}

    def create_job(self, prompt, schedule, name=None, deliver=None, **_kw):
        jid = f"job{len(self.jobs) + 1}"
        self.jobs[jid] = {"id": jid, "prompt": prompt, "schedule": schedule, "name": name, "deliver": deliver,
                          "schedule_display": schedule, "next_run_at": "2026-10-24T02:00:00-04:00"}
        return self.jobs[jid]

    def list_jobs(self, include_disabled=False):
        return list(self.jobs.values())

    def remove_job(self, jid):
        return self.jobs.pop(jid, None) is not None


class Schedule(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir()
        self.cron = FakeCron()
        plugin._cron = lambda: self.cron
        plugin._pending.clear()
        plugin._session = lambda name: {"HERMES_SESSION_PLATFORM": "api_server"}.get(name, "")

    def say(self, text, sid="s1"):
        return plugin._on_pre_llm_call(session_id=sid, user_message=text)

    def test_parsing(self):
        cases = {
            "delete the pomodoro app at 2am": ("2026-10-24T02:00", "delete the pomodoro app"),
            "every morning at 8 summarize my calendar": ("every day at 8am", "summarize my calendar"),
            "in 2 hours check the PC temps": ("in 2 hours", "check the PC temps"),
            "tomorrow at 9am send me the weather": ("2026-10-24T09:00", "send me the weather"),
            "check my PRs every 3 hours": ("every 3h", "check my PRs"),
            "every monday at 9am tell me my week": ("every monday at 9am", "tell me my week"),
            "weekdays at 7:30am give me the news": ("weekdays at 7:30am", "give me the news"),
            "at 9 check the temps": ("2026-10-23T21:00", "check the temps"),
            "can you please schedule a job to back up the vault every night at 1": ("every day at 1am", "back up the vault"),
        }
        for text, (sched, action) in cases.items():
            r = plugin.parse_request(text, NOW)
            self.assertEqual((r["schedule"], r["action"]), (sched, action), text)
        for text in ("I have a meeting at 3pm", "what's on at 2pm?", "remind me at 5 to call mom",
                     "how do I run something every day at 8?", "the build failed at 2am", "at 25 o'clock do x"):
            self.assertIsNone(plugin.parse_request(text, NOW), text)

    def test_asks_then_schedules_on_yes(self):
        r = self.say("delete the pomodoro app at 2am")
        self.assertIn('Schedule "delete the pomodoro app" for', r["context"])
        self.assertIn("also approves that action", r["context"])
        self.assertEqual(self.cron.jobs, {})
        r = self.say("yes")
        self.assertIn("Done: scheduled", r["context"])
        job = self.cron.jobs["job1"]
        self.assertTrue(job["prompt"].startswith("[Scheduled job:"))
        self.assertTrue(job["prompt"].endswith("delete the pomodoro app"))
        self.assertEqual(job["deliver"], "local")
        self.assertTrue(plugin.is_approved_cron("cron:job1:exec9"))
        self.assertIsNone(plugin.is_approved_cron("cron:other:exec9"))
        self.assertIsNone(plugin.is_approved_cron("t1"))

    def test_no_yes_nothing_scheduled(self):
        self.say("every morning at 8 summarize my calendar")
        self.assertIn("nothing was scheduled", self.say("actually never mind")["context"])
        self.assertEqual(self.cron.jobs, {})

    def test_list_and_cancel(self):
        for t in ("every morning at 8 summarize my calendar", "check my PRs every 3 hours"):
            self.say(t)
            self.say("yes")
        r = self.say("what's scheduled?")
        self.assertIn("summarize my calendar", r["context"])
        self.assertIn("check my PRs", r["context"])
        r = self.say("cancel the job")
        self.assertIn("Not sure which", r["context"])
        r = self.say("cancel the calendar job")
        self.assertIn('cancelled "summarize my calendar"', r["context"])
        self.assertEqual(list(self.cron.jobs), ["job2"])
        self.assertNotIn("job1", plugin.load_jobs())

    def test_cron_answer_goes_to_the_phone(self):
        self.say("in 2 hours check the PC temps")
        self.say("yes")
        plugin._on_post_llm_call(task_id="cron:job1:e1", assistant_response="GPU 41 °C, CPU 43 °C. All fine.")
        plugin._on_post_llm_call(task_id="cron:unrelated:e1", assistant_response="x")
        plugin._on_post_llm_call(task_id="chat-1", assistant_response="y")
        n = plugin.notices.after(0)
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["title"], "⏰ check the PC temps")
        self.assertEqual(n[0]["text"], "GPU 41 °C, CPU 43 °C. All fine.")
        self.assertEqual(plugin.notices.after(n[0]["id"]), [])

    def test_only_phone_dm_cli_and_not_inside_cron(self):
        plugin._session = lambda name: {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "group"}.get(name, "")
        self.assertIsNone(self.say("delete the pomodoro app at 2am"))
        plugin._session = lambda name: {"HERMES_SESSION_PLATFORM": "api_server"}.get(name, "")
        self.assertIsNone(plugin._on_pre_llm_call(user_message="delete the pomodoro app at 2am", task_id="cron:job1:x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
