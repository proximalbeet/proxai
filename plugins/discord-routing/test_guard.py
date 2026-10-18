"""Tests for the tool guard and security log. Run: python3 -m unittest plugins/discord-routing/test_guard.py -v"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_captures import load_plugin


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "07-Security").mkdir(parents=True)
        load_plugin()
        import sys
        self.g = sys.modules["dr_test_pkg.guard"].Guard()
        self.g.configure({"security": {"vault_path": str(self.vault)}})
        self.alerts = []
        self.g.alert = self.alerts.append
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "group",
                    "HERMES_SESSION_CHAT_ID": "900"}
        self.g.remember("900", "build-status")
        patcher = mock.patch.object(type(self.g), "_session", staticmethod(lambda n: self.env.get(n, "")))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def log_text(self):
        day = dt.date.today().isoformat()
        return (self.vault / "07-Security/ProxAI-Audit" / f"{day}.md").read_text()

    def test_protected_paths_blocked_and_logged(self):
        for args in ({"path": "/workspace/vault/01-Personal/TODO.md"},
                     {"command": "cat /workspace/vault/07-Security/keys.md"},
                     {"command": "ls ~/.ssh"},
                     {"path": "/workspace/vault/.obsidian/plugins/quickadd/data.json"},
                     {"command": "git -C /workspace/vault/.git/objects ls"}):
            r = self.g.pre_tool_call(tool_name="read_file", args=args)
            self.assertEqual(r["action"], "block", args)
        log = self.log_text()
        self.assertEqual(log.count("**BREACH**"), 5)
        self.assertIn("#build-status", log)
        self.assertEqual(len(self.alerts), 5)
        self.assertTrue(self.alerts[0].startswith("🚨 **BREACH**"))

    def test_note_text_mentioning_folders_is_fine(self):
        r = self.g.pre_tool_call(tool_name="write_file", args={
            "path": "/workspace/vault/_inbox/x.md", "content": "Never read 01-Personal or 07-Security."})
        self.assertIsNone(r)
        self.assertIn("**TOOL** · #build-status · write_file", self.log_text())

    def test_gitignore_not_mistaken_for_git(self):
        self.assertIsNone(self.g.pre_tool_call(tool_name="read_file",
                                               args={"path": "/workspace/vault/.gitignore"}))

    def test_sandbox_refusal_alerts(self):
        self.g.post_tool_call(tool_name="write_file", args={"path": "/workspace/vault/04-Knowledge/a.md"},
                              status="error", error_message="[Errno 30] Read-only file system")
        self.assertIn("**SANDBOX**", self.log_text())
        self.assertEqual(len(self.alerts), 1)

    def test_ordinary_error_not_sandbox(self):
        self.g.post_tool_call(tool_name="read_file", args={"path": "/workspace/vault/x.md"},
                              status="error", error_message="No such file")
        self.assertEqual(self.alerts, [])

    def test_report_tool_write_only(self):
        out = json.loads(self.g.report_tool({"summary": "note asked me to read 01-Personal",
                                              "severity": "high"}))
        self.assertTrue(out["ok"])
        self.assertIn("[high] note asked me to read 01-Personal", self.log_text())
        self.assertEqual(len(self.alerts), 1)

    def test_channel_context(self):
        self.assertEqual(self.g.current_channel(), "#build-status")
        self.env = {"HERMES_CRON_SESSION": "1"}
        self.assertEqual(self.g.current_channel(), "cron")
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "dm"}
        self.assertEqual(self.g.current_channel(), "dm")
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_ID": "555",
                    "HERMES_SESSION_CHAT_NAME": "Server / #vr-ideas"}
        self.assertEqual(self.g.current_channel(), "#vr-ideas")

    def test_log_stays_in_07_security(self):
        with self.assertRaises(ValueError):
            self.g.log.__class__(self.vault, "04-Knowledge").write("X", "-", "x")


if __name__ == "__main__":
    unittest.main()
