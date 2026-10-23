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



class AppRequestHook(Base):
    def hook(self, text):
        return plugin._on_pre_llm_call(session_id="s1", user_message=text)

    def test_names(self):
        for text, name in (("Build a pomodoro timer with a progress ring", "Pomodoro Timer"),
                           ("make me a habit tracker", "Habit Tracker"),
                           ("Can you build me a PC monitor app?", "PC Monitor"),
                           ("Make a grocery list I can check off", "Grocery List"),
                           ("create an app that tracks my water", "")):
            self.assertEqual(plugin.guess_name(text), name, text)

    def test_new_app_request_starts_the_build_itself(self):
        r = self.hook("Build a pomodoro timer with a progress ring")
        self.assertIn("ALREADY started", r["context"])
        self.assertIn("app id pomodoro-timer", r["context"])
        self.wait_idle()
        task = self.cc.calls[0]["args"]["task"]
        self.assertIn("Build a pomodoro timer with a progress ring", task)
        self.assertIn("Name: Pomodoro Timer", task)
        self.assertEqual(len(self.cc.calls), 1)

    def test_second_request_while_building_says_busy(self):
        self.cc.delay = 5
        self.hook("make me a habit tracker")
        r = self.hook("make me a water tracker")
        self.assertIn("can't start yet", r["context"])
        self.cc.release.set()
        self.wait_idle()
        self.assertEqual(len(self.cc.calls), 1)

    def test_change_from_the_apps_tab_starts_an_update(self):
        make_app("pomodoro-timer", name="Pomodoro Timer")
        r = self.hook("Change my Pomodoro Timer app (pomodoro-timer): add a 50-minute focus option")
        self.assertIn("ALREADY started updating Pomodoro Timer", r["context"])
        self.wait_idle()
        task = self.cc.calls[0]["args"]["task"]
        self.assertIn("ALREADY EXISTS", task)
        self.assertIn("add a 50-minute focus option", task)

    def test_plain_change_by_name_starts_an_update(self):
        make_app("pomodoro-timer", name="Pomodoro Timer")
        make_app("water-log", name="Water Log")
        r = self.hook("add a dark mode toggle to my pomodoro timer")
        self.assertIn("pomodoro-timer", r["context"])
        self.wait_idle()
        self.assertIn("/workspace/proxai/apps/pomodoro-timer", self.cc.calls[0]["args"]["task"])

    def test_rollback_from_the_versions_dialog(self):
        make_app("pomodoro-timer", name="Pomodoro Timer", builds=(1, 2))
        r = self.hook("Roll back my Pomodoro Timer app (pomodoro-timer) to build 1 (version 1.0).")
        self.assertIn("rolling Pomodoro Timer back to build 1", r["context"])
        self.wait_idle()
        self.assertEqual(self.shell[-1], "proxai-app-build --rollback 1 /workspace/proxai/apps/pomodoro-timer")
        self.assertEqual(self.cc.calls, [])
        r = self.hook("Roll back my Pomodoro Timer app (pomodoro-timer) to build 9")
        self.assertIn("has no build 9", r["context"])

    def test_unclear_app_gets_a_hint(self):
        make_app("pomodoro-timer", name="Pomodoro Timer")
        r = self.hook("Change my tip calculator app: round up")
        self.assertIn("proxai_app", r["context"])
        self.assertNotIn("ALREADY", r["context"])
        self.assertEqual(self.cc.calls, [])

    def test_mentioning_an_app_without_a_change_does_nothing(self):
        make_app("pomodoro-timer", name="Pomodoro Timer")
        self.assertIsNone(self.hook("I just used my pomodoro timer, it was great"))
        self.assertIsNone(self.hook("How do I add a sound to my pomodoro timer?"))
        self.assertEqual(self.cc.calls, [])

    def test_left_alone(self):
        for text in ("How would I build an app that tracks water?", "What's the weather tomorrow?",
                     "make me a quick timer right here in the chat", "remind me in 20 minutes to check the oven",
                     "make a list of groceries for tomorrow", "turn on server mode"):
            self.assertIsNone(self.hook(text), text)
        self.assertEqual(self.cc.calls, [])

    def test_only_phone_and_cli(self):
        self.env["HERMES_SESSION_PLATFORM"] = "discord"
        self.assertIsNone(self.hook("Build a pomodoro timer"))
        self.env["HERMES_SESSION_PLATFORM"] = "api_server"
        self.assertIsNone(plugin._on_pre_llm_call(session_id="s1", user_message="Build an app", parent_session_id="p"))
        self.assertEqual(self.cc.calls, [])


class DeleteApps(Base):
    def hook(self, text, sid="s1"):
        return plugin._on_pre_llm_call(session_id=sid, user_message=text)

    def setUp(self):
        super().setUp()
        plugin._pending_delete.clear()
        make_app("pomodoro-timer", name="Pomodoro Timer")
        plugin._shell = lambda command, task_id=None, timeout=120: (
            self.shell.append(command) or {"output": "moved" if command.startswith("mkdir -p /workspace/proxai/apps/.trash") else "", "exit_code": 0})

    def test_delete_asks_then_moves_to_trash_on_yes(self):
        r = self.hook("remove or delete my pomodoro timer app entirely")
        self.assertIn("Delete Pomodoro Timer from the PC?", r["context"])
        self.assertEqual(self.cc.calls, [])            # not treated as a change
        self.assertEqual(self.shell, [])
        r = self.hook("yes")
        self.assertIn("was deleted from the PC", r["context"])
        self.assertIn("mv /workspace/proxai/apps/pomodoro-timer /workspace/proxai/apps/.trash/pomodoro-timer-", self.shell[-1])

    def test_anything_but_yes_cancels(self):
        self.hook("delete my pomodoro timer app")
        r = self.hook("wait, no")
        self.assertIn("NOT deleted", r["context"])
        self.assertEqual(self.shell, [])
        self.assertIsNone(self.hook("yes"))            # the request is gone

    def test_other_sessions_cant_confirm(self):
        self.hook("delete my pomodoro timer app")
        self.hook("yes", sid="other")
        self.assertEqual(self.shell, [])

    def test_unclear_app_asks_which(self):
        r = self.hook("delete the app")
        self.assertIn("not clear which", r["context"])
        self.assertIn("Pomodoro Timer (pomodoro-timer)", r["context"])

    def test_short_and_misspelled_names(self):
        # 10-23 on the phone: "delete pomodoro app" and "now uninstall pomdoro timer" weren't recognised.
        for msg in ("delete pomodoro app", "now uninstall pomdoro timer", "get rid of the pomodoro"):
            plugin._pending_delete.clear()
            r = self.hook(msg)
            self.assertIn("Delete Pomodoro Timer from the PC?", r["context"], msg)
        self.assertEqual(self.shell, [])
        self.assertEqual(self.cc.calls, [])

    def test_removing_a_part_is_a_change_not_a_delete(self):
        plugin._pending_delete.clear()
        r = self.hook("remove the reset button from my pomodoro timer app")
        self.assertNotIn("Delete Pomodoro Timer", (r or {}).get("context", ""))
        self.assertEqual(plugin._pending_delete, {})
        self.wait_idle()
        self.assertEqual(len(self.cc.calls), 1)                 # started an update instead
        make_app("pomodoro-timer", name="Pomodoro Timer")      # (the fake builder renamed it)
        r = self.hook("Delete my Pomodoro Timer app (pomodoro-timer) from the PC.")
        self.assertIn("Delete Pomodoro Timer from the PC?", r["context"])

    def test_loose_match_never_starts_builds(self):
        make_app("water-log", name="Water Log")
        self.assertIsNone(plugin.change_request("add a pomdoro button"))   # typo: no build started
        self.assertIsNone(plugin.find_app_loose("delete the timer"))         # generic word only

    def test_old_container_without_model_flag_still_builds(self):
        # 10-23: the live container predated the image with cc-run --model; builds failed in 3 s.
        real = self.cc._handle_claude_code
        def old_cc_run(args, task_id=None, session_id=None):
            if "model" in args:
                self.cc.calls.append(args)
                return json.dumps({"ok": False, "result": "", "error": "unknown argument: --model"})
            return real(args, task_id=task_id, session_id=session_id)
        self.cc._handle_claude_code = old_cc_run
        plugin._start("create", "tip-calc", "a tip calculator", "Tip Calc", None, None)
        self.wait_idle()
        self.assertEqual(json.loads((APPS / "tip-calc" / "status.json").read_text())["state"], "built")
        self.assertNotIn("model", self.cc.calls[-1])

    def test_quick_question_isnt_an_in_chat_app(self):
        # 10-23: "quick question" made the hook skip a real APK request.
        msg = "Build me a standalone pomodoro app. Quick question, will both this apk and the old one stay?"
        self.assertTrue(plugin.is_new_app_request(msg))
        # The phone's time note in front of the message doesn't get in the way.
        r = plugin._on_pre_llm_call(session_id="s9", user_message="[Phone time: Friday 2026-10-23 12:29 "
                                    "(America/Detroit); by voice]\nbuild me a tip calculator")
        self.assertIn("ALREADY", r["context"])
        self.wait_idle()
        for msg in ("make me a quick html page with a timer right here", "build a timer in the chat",
                    "make a tip calculator as an html file"):
            self.assertFalse(plugin.is_new_app_request(msg), msg)

    def test_failed_update_with_no_build_restores_ready(self):
        self.cc.ok = False
        def declined(args, task_id=None, session_id=None):
            self.cc.calls.append(args)
            return json.dumps({"ok": False, "result": "", "error": "I didn't change anything."})
        self.cc._handle_claude_code = declined
        d = APPS / "pomodoro-timer"
        st = json.loads((d / "status.json").read_text())
        st["last_build"] = {"code": 1}
        st["state"] = "queued"
        (d / "status.json").write_text(json.dumps(st))
        plugin._start("update", "pomodoro-timer", "make it purple", "Pomodoro Timer", None, None)
        self.wait_idle()
        self.assertTrue(any("--status built 'No changes made" in c for c in self.shell), self.shell)

if __name__ == "__main__":
    unittest.main(verbosity=2)
