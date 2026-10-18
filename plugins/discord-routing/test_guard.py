"""Tests for the tool guard and security log. Run: python3 -m unittest plugins/discord-routing/test_guard.py -v"""

import datetime as dt
import json
import sys
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

    def use_real_policy(self):
        cfg = json.loads((Path(__file__).resolve().parent / "routing.json").read_text())
        cfg["security"]["vault_path"] = str(self.vault)
        self.g.configure(cfg)
        toolsets = {"web_search": "web", "terminal": "terminal", "write_file": "file", "read_file": "file",
                    "discord_admin": "discord_admin", "delegate_task": "delegation"}
        patcher = mock.patch.object(sys.modules["dr_test_pkg.guard"], "_toolset_of",
                                    lambda n: toolsets.get(n, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def at(self, channel):
        self.g.remember("900", channel)

    def test_log_channel_gets_no_tools(self):
        self.use_real_policy()
        self.at("build-status")
        r = self.g.pre_tool_call(tool_name="write_file", args={"path": "/workspace/vault/_inbox/x.md"})
        self.assertEqual(r["action"], "block")
        self.assertIn("isn't allowed in #build-status", r["message"])
        self.assertIn("**BLOCKED**", self.log_text())
        self.assertTrue(self.alerts[-1].startswith("⛔"))

    def test_capture_channel_reads_but_cant_write(self):
        self.use_real_policy()
        self.at("focus-sessions")
        self.assertIsNone(self.g.pre_tool_call(tool_name="search_files",
                                               args={"pattern": "focus-log", "path": "/workspace/vault"}))
        for tool in ("write_file", "terminal", "delegate_task"):
            self.assertEqual(self.g.pre_tool_call(tool_name=tool, args={})["action"], "block", tool)

    def test_toolset_names_allow_their_tools(self):
        self.use_real_policy()
        self.at("code-assist")
        self.assertIsNone(self.g.pre_tool_call(tool_name="terminal", args={"command": "pytest"}))
        self.at("general-chat")
        self.assertIsNone(self.g.pre_tool_call(tool_name="web_search", args={"query": "x"}))
        self.assertEqual(self.g.pre_tool_call(tool_name="terminal", args={})["action"], "block")

    def test_unknown_channel_is_read_only(self):
        self.use_real_policy()
        self.at("brand-new-channel")
        self.assertIsNone(self.g.pre_tool_call(tool_name="read_file", args={"path": "/workspace/vault/a.md"}))
        self.assertEqual(self.g.pre_tool_call(tool_name="write_file", args={})["action"], "block")

    def test_dm_and_cron_unlimited_but_denylist_applies(self):
        self.use_real_policy()
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "dm"}
        self.assertIsNone(self.g.pre_tool_call(tool_name="terminal", args={}))
        self.env = {"HERMES_CRON_SESSION": "1"}
        self.assertIsNone(self.g.pre_tool_call(tool_name="terminal", args={"command": "git log"}))
        self.assertEqual(self.g.pre_tool_call(tool_name="discord_admin", args={})["action"], "block")

    def test_report_tool_always_allowed(self):
        self.use_real_policy()
        self.at("announcements")
        self.assertIsNone(self.g.pre_tool_call(tool_name="report_security_event", args={"summary": "x"}))

    # ---- request trimming (llm_request middleware) --------------------------

    DIRECT = ["read_file", "search_files", "write_file", "terminal", "web_search", "delegate_task",
              "text_to_speech", "clarify", "tool_search", "tool_describe", "tool_call"]

    def anthropic_request(self, cache_last=True):
        tools = [{"name": n, "description": "d", "input_schema": {}} for n in self.DIRECT]
        if cache_last:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return {"model": "claude", "messages": [{"role": "user", "content": "hi"}], "tools": tools,
                "tool_choice": {"type": "auto"}}

    def names(self, out):
        return [t.get("name") or t["function"]["name"] for t in out["request"].get("tools", [])]

    def use_trim_policy(self):
        self.use_real_policy()
        toolsets = {"web_search": "web", "terminal": "terminal", "write_file": "file", "read_file": "file",
                    "search_files": "file", "delegate_task": "delegation", "text_to_speech": "tts",
                    "clarify": "clarify", "discord_admin": "discord_admin"}
        patcher = mock.patch.object(sys.modules["dr_test_pkg.guard"], "_toolset_of",
                                    lambda n: toolsets.get(n, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_capture_channel_gets_only_its_tools(self):
        self.use_trim_policy()
        self.at("brain-dump")
        out = self.g.trim_tools(request=self.anthropic_request())
        # capture = read_file, search_files, clarify, tts; all sent directly, so no Tool Search
        self.assertEqual(self.names(out), ["read_file", "search_files", "text_to_speech", "clarify"])
        self.assertEqual(out["request"]["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("#brain-dump", out["reason"])

    def test_log_channel_sends_no_tools(self):
        self.use_trim_policy()
        self.at("build-status")
        out = self.g.trim_tools(request=self.anthropic_request())
        self.assertNotIn("tools", out["request"])
        self.assertNotIn("tool_choice", out["request"])

    def test_no_tools_but_history_has_tool_calls_leaves_request(self):
        self.use_trim_policy()
        self.at("build-status")
        req = self.anthropic_request()
        req["messages"].append({"role": "assistant", "content": [{"type": "tool_use", "name": "x"}]})
        self.assertIsNone(self.g.trim_tools(request=req))

    def test_code_channel_keeps_tool_search_for_claude_code(self):
        self.use_trim_policy()
        self.at("code-assist")
        req = self.anthropic_request()
        req["tools"][:0] = [{"name": "discord_admin", "input_schema": {}},
                            {"name": "browser_exec", "input_schema": {}}]
        names = self.names(self.g.trim_tools(request=req))
        self.assertNotIn("browser_exec", names)
        self.assertIn("tool_call", names)       # claude_code is reached through tool_call
        self.assertIn("terminal", names)
        self.assertNotIn("discord_admin", names)

    def test_dm_unlimited_untouched(self):
        self.use_trim_policy()
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "dm"}
        self.assertIsNone(self.g.trim_tools(request=self.anthropic_request()))

    def test_deny_everywhere_removed_even_in_dm(self):
        self.use_trim_policy()
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "dm"}
        req = self.anthropic_request()
        req["tools"].insert(0, {"name": "discord_admin", "input_schema": {}})
        self.assertNotIn("discord_admin", self.names(self.g.trim_tools(request=req)))

    def test_unknown_context_untouched(self):
        self.use_trim_policy()
        self.env = {}
        self.assertIsNone(self.g.trim_tools(request=self.anthropic_request()))

    def test_openai_shape(self):
        self.use_trim_policy()
        self.at("vault-queries")
        req = {"messages": [], "tools": [{"type": "function", "function": {"name": n}} for n in self.DIRECT]}
        names = self.names(self.g.trim_tools(request=req))
        # vault = read_file, search_files, write_file, clarify, tts, session_search (not sent → Tool Search)
        self.assertEqual(names, ["read_file", "search_files", "write_file", "text_to_speech", "clarify",
                                 "tool_search", "tool_describe", "tool_call"])

    def test_tool_choice_naming_removed_tool_falls_back_to_auto(self):
        self.use_trim_policy()
        self.at("brain-dump")
        req = self.anthropic_request()
        req["tool_choice"] = {"type": "tool", "name": "terminal"}
        self.assertEqual(self.g.trim_tools(request=req)["request"]["tool_choice"], {"type": "auto"})

    def test_prompt_lines(self):
        self.use_real_policy()
        self.assertIn("no tools are available", self.g.policy.prompt_line("#announcements"))
        self.assertIn("read_file, search_files", self.g.policy.prompt_line("#brain-dump"))
        self.assertEqual(self.g.policy.prompt_line("dm"), "")

    def test_log_stays_in_07_security(self):
        with self.assertRaises(ValueError):
            self.g.log.__class__(self.vault, "04-Knowledge").write("X", "-", "x")


if __name__ == "__main__":
    unittest.main()
