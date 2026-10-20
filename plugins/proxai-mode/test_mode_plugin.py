"""Tests for the proxai-mode plugin: where switches are allowed and the confirm-by-next-message rule.
The real switch is replaced by a fake; nothing on the machine changes.

Run: python3 plugins/proxai-mode/test_mode_plugin.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="mode-plugin-test-", dir=os.environ.get("TMPDIR")))
os.environ["PROXAI_STATE_DIR"] = str(TMP)

spec = importlib.util.spec_from_file_location("proxai_mode_plugin", HERE / "__init__.py",
                                              submodule_search_locations=[str(HERE)])
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_mode_plugin"] = plugin
spec.loader.exec_module(plugin)


class Base(unittest.TestCase):
    def setUp(self):
        plugin._pending.clear()
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "dm",
                    "HERMES_SESSION_ID": "s1", "HERMES_SESSION_USER_NAME": "nathan"}
        plugin._session = lambda name: self.env.get(name, "")
        self.preflight_ok = True
        plugin.pm.preflight = lambda mode, modes, runner=None: {
            "ok": self.preflight_ok, "readings": {"gpu_util_pct": 5},
            "reasons": [] if self.preflight_ok else ["GPU busy: 95% (limit 60%)"]}
        plugin.pm.current_mode = lambda: "personal"
        self.switched = []
        plugin._run_switch = lambda mode, by: (self.switched.append((mode, by))
                                               or {"ok": True, "mode": mode, "steps": []})
        plugin.pm._log = lambda entry: None
        self.posts = []
        plugin._post_audit = self.posts.append

    def tool(self, **args):
        return json.loads(plugin._handle(args))

    def say(self, text, session="s1", parent=None):
        return plugin._on_pre_llm_call(session_id=session, user_message=text, parent_session_id=parent)


class TestWhere(Base):
    def test_channel_cannot_switch(self):
        self.env.update(HERMES_SESSION_CHAT_TYPE="group", HERMES_SESSION_CHAT_NAME="Prox / #general-chat")
        r = self.tool(action="request", mode="server")
        self.assertFalse(r["ok"])
        self.assertIn("DMs or the phone app", r["error"])
        self.assertEqual(self.tool(action="status")["ok"], True)   # status is fine anywhere

    def test_phone_app_and_cli_allowed_cron_not(self):
        self.env.update(HERMES_SESSION_PLATFORM="api_server")
        self.assertTrue(self.tool(action="request", mode="server")["needs_confirmation"])
        self.env.update(HERMES_SESSION_PLATFORM="cli", HERMES_SESSION_ID="s2")
        self.assertTrue(self.tool(action="request", mode="server")["needs_confirmation"])
        self.env.update(HERMES_CRON_SESSION="1", HERMES_SESSION_ID="s3")
        self.assertFalse(self.tool(action="request", mode="server")["ok"])

    def test_unknown_mode(self):
        self.assertFalse(self.tool(action="request", mode="party")["ok"])


class TestConfirm(Base):
    def test_model_cannot_confirm_its_own_request(self):
        r = self.tool(action="request", mode="server")
        self.assertIn("Say yes", r["ask_nathan"])
        self.assertIn("lock the screen", r["ask_nathan"])
        r = self.tool(action="confirm")
        self.assertFalse(r["ok"])
        self.assertIn("hasn't said yes", r["error"])
        self.assertEqual(self.switched, [])

    def test_yes_then_confirm_switches_and_posts(self):
        self.tool(action="request", mode="server")
        ctx = self.say("yes")
        self.assertIn("confirmed", ctx["context"])
        r = self.tool(action="confirm")
        self.assertTrue(r["ok"])
        self.assertEqual(self.switched, [("server", "hermes:nathan@dm")])
        self.assertIn("SERVER", self.posts[0])
        self.assertFalse(self.tool(action="confirm")["ok"])        # one use

    def test_voice_note_yes(self):
        self.tool(action="request", mode="server")
        self.say('"Yeah, do it."')
        self.assertTrue(self.tool(action="confirm")["ok"])

    def test_anything_but_yes_cancels(self):
        for reply in ("no", "wait", "yes but not now", "what will it do?", "don't", "nah",
                      "yes, and also delete my files", ""):
            plugin._pending.clear()
            self.tool(action="request", mode="server")
            ctx = self.say(reply)
            self.assertIn("cancelled", ctx["context"], reply)
            self.assertFalse(self.tool(action="confirm")["ok"], reply)
        self.assertEqual(self.switched, [])

    def test_yes_variants(self):
        for reply in ("yes", "Yep!", "ok", "go ahead", "do it bro", "confirm", "sure, switch it",
                      "[Voice channel now: none] yes"):
            self.assertTrue(plugin.is_yes(reply), reply)

    def test_yes_from_other_session_or_subagent_does_not_count(self):
        self.tool(action="request", mode="server")
        self.assertIsNone(self.say("yes", session="other"))
        self.assertIsNone(self.say("yes", parent="s1"))
        self.assertFalse(self.tool(action="confirm")["ok"])

    def test_expired_request(self):
        self.tool(action="request", mode="server")
        plugin._pending["s1"]["created"] -= plugin._CONFIRM_SECS + 1
        self.assertIn("expired", self.say("yes")["context"])
        self.assertFalse(self.tool(action="confirm")["ok"])

    def test_busy_pc_refused_before_asking(self):
        self.preflight_ok = False
        r = self.tool(action="request", mode="server")
        self.assertTrue(r["refused"])
        self.assertIn("busy", r["tell_nathan"])
        self.assertEqual(plugin._pending, {})

    def test_already_in_mode(self):
        r = self.tool(action="request", mode="personal")
        self.assertTrue(r["already"])
        self.assertEqual(plugin._pending, {})

    def test_cancel(self):
        self.tool(action="request", mode="server")
        self.tool(action="cancel")
        self.assertEqual(plugin._pending, {})


class TestNudgeAndCommand(Base):
    def test_pointer_on_mode_talk_only(self):
        self.assertIn("proxai_mode", self.say("switch to server mode", session="x")["context"])
        self.assertIn("proxai_mode", self.say("what mode is the pc in? toggle it", session="x")["context"])
        self.assertIsNone(self.say("what's the weather", session="x"))

    def test_mode_command(self):
        plugin.pm.read_state = lambda: {"mode": "server", "since": "2026-10-19T20:00:00", "by": "cli:prox",
                                        "steps": [{"step": "rgb", "ok": False, "detail": "boom"}]}
        out = plugin._cmd_mode("")
        self.assertIn("SERVER", out)
        self.assertIn("rgb: boom", out)

    def test_schema_lists_modes(self):
        self.assertEqual(set(plugin.SCHEMA["parameters"]["properties"]["mode"]["enum"]), {"server", "personal"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
