"""Tests for proxai-github with a fake GitHub (no network).

Run: python3 plugins/proxai-github/test_github.py
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
TMP = Path(tempfile.mkdtemp(prefix="github-test-", dir=os.environ.get("TMPDIR")))
os.environ.update(PROXAI_GITHUB_SECRETS=str(TMP / "github.json"), PROXAI_GITHUB_LOG=str(TMP / "github.log"),
                  PROXAI_VAULT=str(TMP / "vault"))
spec = importlib.util.spec_from_file_location("proxai_github_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_github_plugin"] = plugin
spec.loader.exec_module(plugin)


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, *, token, body=None, query=None, raw=False):
        self.calls.append((method, path, token, body))
        if path == "/repos/proximalbeet/proxai":
            return {"full_name": "proximalbeet/proxai", "private": True, "default_branch": "main",
                    "description": "d", "open_issues_count": 2, "html_url": "u"}
        if path.endswith("/branches"):
            return [{"name": "main"}, {"name": "worktree-proxai-apps"}]
        if path.endswith("/pulls") and method == "GET":
            return [{"number": 4, "title": "Apps", "state": "open", "user": {"login": "proximalbeet"},
                     "updated_at": "t", "draft": False, "html_url": "u4"}]
        if "/contents/" in path and method == "GET":
            if raw:
                return "# ProxAI\n"
            raise plugin.Refused("GitHub said 404: Not Found")
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "abc123"}}
        if path.endswith("/git/refs") and method == "POST":
            return {"ref": body["ref"]}
        if path.endswith("/issues") and method == "POST":
            return {"html_url": "https://github.com/proximalbeet/proxai/issues/9", "number": 9}
        if "/contents/" in path and method == "PUT":
            return {"commit": {"sha": "def4567890"}, "content": {"html_url": "f"}}
        raise AssertionError(f"unexpected {method} {path}")


class GitHubTool(unittest.TestCase):
    def setUp(self):
        (TMP / "github.json").write_text(json.dumps({"read": "R", "write": "W"}))
        plugin._pending.clear()
        self.gh = FakeGitHub()
        plugin._request = self.gh
        self.env = {"HERMES_SESSION_PLATFORM": "api_server", "HERMES_SESSION_ID": "s1"}
        plugin._session = lambda name: self.env.get(name, "")
        plugin._audit = lambda text, where: self.audit.append(text)
        self.audit = []

    def call(self, **a) -> dict:
        return json.loads(plugin._handle(a))

    def say(self, text):
        return plugin._on_pre_llm_call(session_id="s1", user_message=text)

    def test_reads_use_the_read_token_and_need_no_yes(self):
        r = self.call(action="repo", repo="proxai")
        self.assertEqual(r["result"]["branches"], ["main", "worktree-proxai-apps"])
        self.assertEqual(self.call(action="prs", repo="proximalbeet/proxai")["result"][0]["number"], 4)
        self.assertEqual(self.call(action="file", repo="proxai", path="README.md")["result"]["content"], "# ProxAI\n")
        self.assertTrue(all(c[2] == "R" and c[0] == "GET" for c in self.gh.calls))

    def test_write_asks_then_runs_only_after_yes(self):
        r = self.call(action="create_issue", repo="proxai", title="Cron for delayed deletes", body="x")
        self.assertTrue(r["needs_confirmation"])
        self.assertIn("open an issue in proximalbeet/proxai", r["ask_nathan"])
        self.assertFalse(self.call(action="confirm")["ok"])           # can't approve itself
        self.assertEqual([c for c in self.gh.calls if c[0] != "GET"], [])
        self.assertIn("Nathan said yes", self.say("yes")["context"])
        r = self.call(action="confirm")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["number"], 9)
        self.assertEqual([c[2] for c in self.gh.calls if c[0] == "POST"], ["W"])
        self.assertIn("approved", self.audit[-1])

    def test_anything_but_yes_cancels(self):
        self.call(action="create_issue", repo="proxai", title="t")
        self.assertIn("NOT done", self.say("yes but change the title first")["context"])
        self.assertFalse(self.call(action="confirm")["ok"])

    def test_never_his_branches_main_or_workflows(self):
        for a in ({"action": "create_branch", "repo": "proxai", "branch": "main"},
                  {"action": "commit_file", "repo": "proxai", "branch": "worktree-proxai-apps", "path": "x", "content": "y"},
                  {"action": "commit_file", "repo": "proxai", "branch": "proxai/../main", "path": "x", "content": "y"},
                  {"action": "commit_file", "repo": "proxai", "branch": "proxai/fix", "path": ".github/workflows/x.yml",
                   "content": "y"},
                  {"action": "open_pr", "repo": "proxai", "branch": "feature", "title": "t"},
                  {"action": "merge", "repo": "proxai"}):
            r = self.call(**a)
            self.assertFalse(r["ok"], a)
        self.assertEqual(plugin._pending, {})
        self.assertEqual(self.gh.calls, [])

    def test_branch_and_commit_on_proxai_branch(self):
        self.call(action="create_branch", repo="proxai", branch="proxai/cron-idea")
        self.say("yes")
        self.assertEqual(self.call(action="confirm")["result"], {"branch": "proxai/cron-idea", "from": "main"})
        self.call(action="commit_file", repo="proxai", branch="proxai/cron-idea", path="docs/idea.md",
                  content="# Idea\n", message="idea")
        self.say("go ahead")
        r = self.call(action="confirm")
        self.assertEqual(r["result"]["commit"], "def4567")
        put = [c for c in self.gh.calls if c[0] == "PUT"][0]
        self.assertEqual(put[3]["branch"], "proxai/cron-idea")

    def test_channels_cron_and_missing_tokens(self):
        self.env.update(HERMES_SESSION_PLATFORM="discord", HERMES_SESSION_CHAT_TYPE="group",
                        HERMES_SESSION_CHAT_NAME="general-chat")
        self.assertIn("DMs, the phone app", self.call(action="create_issue", repo="proxai", title="t")["error"])
        self.assertTrue(self.call(action="repo", repo="proxai")["ok"])          # reads still fine
        (TMP / "github.json").write_text(json.dumps({"read": "R"}))
        self.env["HERMES_SESSION_PLATFORM"] = "api_server"
        self.assertIn("isn't set up", self.call(action="comment", repo="proxai", number=1, body="b")["error"])
        (TMP / "github.json").unlink()
        self.assertIn("isn't set up", self.call(action="prs", repo="proxai")["error"])

    def test_hint_on_github_talk_only(self):
        self.assertIn("github tool", self.say("what PRs are open on proxai?")["context"])
        self.assertIsNone(self.say("what's my GPU temp"))

    def test_repo_names(self):
        self.assertEqual(plugin._repo("ProxMobile"), "proximalbeet/ProxMobile")
        self.assertEqual(plugin._repo("https://github.com/a/b/"), "a/b")
        with self.assertRaises(plugin.Refused):
            plugin._repo("../etc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
