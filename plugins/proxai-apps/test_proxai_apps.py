"""Tests for the proxai-apps plugin. Claude Code and the sandbox shell are replaced by fakes;
the real end-to-end build is sandbox/test_app_build.py (builder) + the go-live check.

Run: python3 plugins/proxai-apps/test_proxai_apps.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="proxai-apps-test-", dir=os.environ.get("TMPDIR")))
os.environ["PROXAI_APPS_DIR"] = str(TMP / "apps")
os.environ["PROXAI_STATE_DIR"] = str(TMP / "state")

spec = importlib.util.spec_from_file_location("proxai_apps_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_apps_plugin"] = plugin
spec.loader.exec_module(plugin)
APPS = Path(os.environ["PROXAI_APPS_DIR"])


def make_app(app_id, name="Tip Calc", state="built", builds=(1,)):
    d = APPS / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "proxai-app.json").write_text(json.dumps({"id": app_id, "name": name, "version": "1.0", "description": "x"}))
    (d / "status.json").write_text(json.dumps({"state": state, "message": "m", "time": "2026-10-22T22:00:00"}))
    (d / "builds.json").write_text(json.dumps([{"code": c, "version": "1.0", "time": "t"} for c in builds]))
    return d


class FakeCC:
    """Stands in for claude-code-delegation: 'builds' the app by writing its status files."""

    def __init__(self, ok=True, delay=0.0):
        self.calls, self.ok, self.delay = [], ok, delay
        self.release = threading.Event()

    def _handle_claude_code(self, args, task_id=None, session_id=None):
        self.calls.append({"args": args, "task_id": task_id, "session_id": session_id})
        if self.delay:
            self.release.wait(self.delay)
        app_id = args["task"].split("(app id: ")[1].split(")")[0]
        make_app(app_id, state="built" if self.ok else "testing")
        return json.dumps({"ok": self.ok, "result": "Built it." if self.ok else "", "error": None if self.ok else "tests failed"})


class Base(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(APPS, ignore_errors=True)
        APPS.mkdir(parents=True)
        plugin._job = None
        plugin._last.clear()
        self.env = {"HERMES_SESSION_PLATFORM": "api_server", "HERMES_SESSION_USER_NAME": "nathan"}
        plugin._session = lambda name: self.env.get(name, "")
        self.cc = FakeCC()
        plugin._cc_module = self.cc
        self.shell = []
        plugin._shell = lambda command, task_id=None, timeout=120: (
            self.shell.append(command) or {"output": '{"ok": true, "code": 2}', "exit_code": 0})

    def call(self, **args):
        return json.loads(plugin._handle(args, task_id="t1"))

    def wait_idle(self, timeout=5):
        end = time.time() + timeout
        while plugin._job is not None and time.time() < end:
            time.sleep(0.02)
        self.assertIsNone(plugin._job, "job didn't finish")


class Ids(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(plugin.slug("Tip Calculator!"), "tip-calculator")
        self.assertEqual(plugin.slug("123 Go"), "go")
        self.assertEqual(plugin.slug("!!"), "app")
        self.assertTrue(plugin.ID_RE.match(plugin.slug("A" * 80)))

    def test_new_id_avoids_existing(self):
        make_app("tip-calc")
        self.assertEqual(plugin.new_id("Tip Calc"), "tip-calc-2")


class Create(Base):
    def test_create_runs_claude_code_in_the_background(self):
        r = self.call(action="create", request="a tip calculator that splits the bill", name="Tip Calc")
        self.assertTrue(r["ok"] and r["started"], r)
        self.assertEqual(r["app"], "tip-calc")
        self.assertIn("Apps tab", r["tell_nathan"])
        self.wait_idle()
        call = self.cc.calls[0]
        self.assertEqual(call["args"]["project"], "apps")
        self.assertEqual(call["session_id"], "proxai-app-tip-calc")   # chat messages don't stop the build
        self.assertEqual(call["task_id"], "t1")
        task = call["args"]["task"]
        for part in ("/opt/proxai-shell/GUIDE.md", "/workspace/proxai/apps/tip-calc", "NEW app",
                     "splits the bill", "proxai-app-build /workspace/proxai/apps/tip-calc"):
            self.assertIn(part, task)
        self.assertIn("--status queued 'Starting: Tip Calc' /workspace/proxai/apps/tip-calc", self.shell[0])
        self.assertTrue(plugin._last["tip-calc"]["ok"])
        log = (Path(os.environ["PROXAI_STATE_DIR"]) / "apps.log").read_text()
        self.assertIn('"app": "tip-calc"', log)

    def test_one_build_at_a_time(self):
        self.cc.delay = 5
        self.assertTrue(self.call(action="create", request="app one", name="One")["ok"])
        r = self.call(action="create", request="app two", name="Two")
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])
        self.assertIn("One", r["error"])
        self.cc.release.set()
        self.wait_idle()
        self.assertTrue(self.call(action="create", request="app two", name="Two")["ok"])
        self.wait_idle()

    def test_failed_build_marks_status_failed(self):
        self.cc.ok = False
        self.call(action="create", request="something", name="Broken")
        self.wait_idle()
        self.assertFalse(plugin._last["broken"]["ok"])
        self.assertTrue(any("--status failed" in c for c in self.shell))

    def test_names_are_shell_quoted(self):
        self.call(action="create", request="x", name="Evil $(rm -rf /) `x`")
        self.wait_idle()
        self.assertIn("'Starting: Evil $(rm -rf /) `x`'", self.shell[0])

    def test_only_from_the_phone_or_cli(self):
        self.env["HERMES_SESSION_PLATFORM"] = "discord"
        r = self.call(action="create", request="x")
        self.assertFalse(r["ok"])
        self.assertIn("ProxMobile", r["error"])
        self.env["HERMES_SESSION_PLATFORM"] = "cli"
        self.assertTrue(self.call(action="create", request="x", name="Cli App")["ok"])
        self.wait_idle()
        self.env["HERMES_CRON_SESSION"] = "1"
        self.assertFalse(self.call(action="create", request="x")["ok"])

    def test_empty_request_refused(self):
        self.assertFalse(self.call(action="create", request="  ")["ok"])
        self.assertEqual(self.cc.calls, [])


class UpdateRollbackStatus(Base):
    def test_update_existing_app(self):
        make_app("tip-calc")
        r = self.call(action="update", app="tip-calc", request="add dark mode")
        self.assertTrue(r["ok"], r)
        self.wait_idle()
        task = self.cc.calls[0]["args"]["task"]
        self.assertIn("ALREADY EXISTS", task)
        self.assertIn("add dark mode", task)
        self.assertFalse(self.call(action="update", app="nope", request="x")["ok"])
        self.assertFalse(self.call(action="update", app="../etc", request="x")["ok"])

    def test_rollback_runs_the_builder(self):
        make_app("tip-calc", builds=(1, 2))
        self.assertFalse(self.call(action="rollback", app="tip-calc", code=7)["ok"])
        r = self.call(action="rollback", app="tip-calc", code=1)
        self.assertTrue(r["ok"], r)
        self.wait_idle()
        self.assertEqual(self.shell[-1], "proxai-app-build --rollback 1 /workspace/proxai/apps/tip-calc")
        self.assertEqual(self.cc.calls, [])

    def test_status_and_list(self):
        make_app("tip-calc", builds=(1, 2))
        make_app("habit-log", name="Habits", state="testing")
        (APPS / ".signing").mkdir()
        s = self.call(action="status", app="tip-calc")
        self.assertEqual((s["name"], s["state"], [b["code"] for b in s["builds"]]), ("Tip Calc", "built", [1, 2]))
        apps = self.call(action="list")["apps"]
        self.assertEqual([a["app"] for a in apps], ["habit-log", "tip-calc"])
        self.assertFalse(self.call(action="status", app="nope")["ok"])

    def test_list_and_status_work_from_discord(self):
        self.env["HERMES_SESSION_PLATFORM"] = "discord"
        make_app("tip-calc")
        self.assertTrue(self.call(action="list")["ok"])



class AppRequestHint(Base):
    def hint(self, text):
        r = plugin._on_pre_llm_call(session_id="s1", user_message=text)
        return bool(r and "proxai_app" in r["context"])

    def test_app_requests_get_the_hint(self):
        for text in ("Build a pomodoro timer with a progress ring", "make me a habit tracker",
                     "Can you create an app that tracks my water?", "Make a grocery list I can check off",
                     "build a workout interval timer that beeps", "make me a PC monitor app",
                     "Change my Water Log app (water-log): add a 750 ml button",
                     "roll back my tip calc app to build 1",
                     [{"type": "text", "text": "Build me a calculator"}]):
            self.assertTrue(self.hint(text), text)

    def test_other_messages_dont(self):
        for text in ("What's the weather tomorrow?", "remind me in 20 minutes to check the oven",
                     "make me a quick timer right here in the chat", "how does the app builder work?",
                     "what did we build today?", "turn on server mode", "make a list of groceries for tomorrow"):
            self.assertFalse(self.hint(text), text)

    def test_only_phone_and_cli(self):
        self.env["HERMES_SESSION_PLATFORM"] = "discord"
        self.assertFalse(self.hint("Build a pomodoro timer"))
        self.env["HERMES_SESSION_PLATFORM"] = "api_server"
        self.assertIsNone(plugin._on_pre_llm_call(session_id="s1", user_message="Build an app", parent_session_id="p"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
