"""Tests for proxai-vault-write: folder rules, the confirm-by-next-message rule, and the writer job.
Everything runs against a throwaway vault in a temp folder; the real vault is never touched.

Run:                python3 plugins/proxai-vault-write/test_vault_write.py
Also run the real
sandboxed job:      PROXAI_TEST_SYSTEMD=1 python3 plugins/proxai-vault-write/test_vault_write.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(tempfile.mkdtemp(prefix="vault-write-test-", dir=os.environ.get("TMPDIR")))
VAULT, STATE = ROOT / "vault", ROOT / "state"
os.environ["PROXAI_VAULT"] = str(VAULT)
os.environ["PROXAI_STATE_DIR"] = str(STATE)

spec = importlib.util.spec_from_file_location("proxai_vault_write_plugin", HERE / "__init__.py",
                                              submodule_search_locations=[str(HERE)])
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_vault_write_plugin"] = plugin
spec.loader.exec_module(plugin)
rules = plugin.rules
import apply as applier  # noqa: E402  (HERE is on sys.path via the plugin)


def fresh_vault():
    import shutil
    shutil.rmtree(ROOT, ignore_errors=True)
    for d in ("_inbox", "05-Brain-Dump", "10-Wiki", "04-Knowledge", "01-Personal", "07-Security/ProxAI-Audit"):
        (VAULT / d).mkdir(parents=True, exist_ok=True)
    (VAULT / "01-Personal" / "secret.md").write_text("private\n")
    (VAULT / "04-Knowledge" / "rust.md").write_text("# Rust\nline 2\n")
    STATE.mkdir(parents=True, exist_ok=True)


def run_apply_no_systemd(req: dict) -> dict:
    """Same as the plugin's _run_apply, minus systemd: runs apply.py as a separate process."""
    req_dir = STATE / "vault-write" / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    f = req_dir / f"{req['id']}.json"
    f.write_text(json.dumps(req))
    subprocess.run([sys.executable, str(HERE / "apply.py"), str(f)], capture_output=True, text=True,
                   env={**os.environ}, timeout=30)
    return json.loads((STATE / "vault-write" / "results" / f"{req['id']}.json").read_text())


class Base(unittest.TestCase):
    def setUp(self):
        fresh_vault()
        plugin._pending.clear()
        plugin._security_log = None
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "dm",
                    "HERMES_SESSION_ID": "s1", "HERMES_SESSION_USER_NAME": "nathan"}
        plugin._session = lambda name: self.env.get(name, "")
        self.jobs = []
        plugin._run_apply = lambda req: (self.jobs.append(req) or run_apply_no_systemd(req))

    def call(self, **args) -> dict:
        return json.loads(plugin._handle(args))

    def audit(self) -> str:
        files = list((VAULT / "07-Security" / "ProxAI-Audit").glob("*.md"))
        return files[0].read_text() if files else ""


class Rules(unittest.TestCase):
    def setUp(self):
        self.p = rules.load_policy()

    def test_paths_are_normalised(self):
        self.assertEqual(rules.normalise("/workspace/vault/04-Knowledge/x.md"), "04-Knowledge/x.md")
        self.assertEqual(rules.normalise(f"{VAULT}/02-Projects/a.md"), "02-Projects/a.md")
        self.assertEqual(rules.normalise("04-Knowledge//x.md"), "04-Knowledge/x.md")
        for bad in ("../etc/passwd", "04-Knowledge/../01-Personal/x.md", "/etc/passwd", "~/x.md", "", "./x.md"):
            with self.assertRaises(rules.Refused, msg=bad):
                rules.normalise(bad)

    def test_folder_rules(self):
        r = lambda p: rules.rule_for(p, self.p)
        self.assertEqual(r("_inbox/a.md"), "free")
        self.assertEqual(r("05-Brain-Dump/a.md"), "free")
        self.assertEqual(r("10-Wiki/concepts/a.md"), "free")
        self.assertEqual(r("04-Knowledge/a.md"), "ask")
        self.assertEqual(r("00-Daily/2026-10-22.md"), "ask")
        self.assertEqual(r("notes-at-root.md"), "ask")
        self.assertEqual(r("_inbox-evil/a.md"), "ask")          # prefix of a free folder isn't free
        for never in ("01-Personal/a.md", "07-Security/ProxAI-Audit/x.md", "AGENTS.md", "CLAUDE.md",
                      ".obsidian/app.json", "04-Knowledge/.hidden.md", ".git/config"):
            self.assertEqual(r(never), "never", never)

    def test_file_type_size_and_how(self):
        with self.assertRaises(rules.Refused):
            rules.check("04-Knowledge/run.sh", "x", "create", self.p)
        with self.assertRaises(rules.Refused):
            rules.check("04-Knowledge/a.md", "x" * (self.p["max_bytes"] + 1), "create", self.p)
        with self.assertRaises(rules.Refused):
            rules.check("04-Knowledge/a.md", "   ", "create", self.p)
        with self.assertRaises(rules.Refused):
            rules.check("04-Knowledge/a.md", "x", "delete", self.p)
        self.assertEqual(rules.check("04-Knowledge/a.MD", "x", "create", self.p), ("04-Knowledge/a.MD", "ask"))


class Applier(unittest.TestCase):
    """apply.py on its own: it re-checks everything even if the plugin were wrong."""

    def setUp(self):
        fresh_vault()

    def req(self, path, content="hello", how="create", approved_by=None):
        return {"id": os.urandom(6).hex(), "path": path, "content": content, "how": how,
                "approved_by": approved_by}

    def test_free_folder_create_and_no_overwrite(self):
        r = run_apply_no_systemd(self.req("_inbox/new.md", "# New"))
        self.assertTrue(r["ok"], r)
        self.assertEqual((VAULT / "_inbox/new.md").read_text(), "# New\n")
        r = run_apply_no_systemd(self.req("_inbox/new.md", "again"))
        self.assertFalse(r["ok"])
        self.assertIn("already exists", r["error"])

    def test_ask_folder_needs_approval(self):
        r = run_apply_no_systemd(self.req("04-Knowledge/go.md"))
        self.assertFalse(r["ok"])
        self.assertIn("approval", r["error"])
        self.assertFalse((VAULT / "04-Knowledge/go.md").exists())
        r = run_apply_no_systemd(self.req("04-Knowledge/go.md", approved_by="nathan@dm"))
        self.assertTrue(r["ok"], r)

    def test_never_even_with_approval(self):
        for path in ("01-Personal/secret.md", "07-Security/x.md", "CLAUDE.md"):
            r = run_apply_no_systemd(self.req(path, "pwned", "replace", approved_by="nathan@dm"))
            self.assertFalse(r["ok"], path)
        self.assertEqual((VAULT / "01-Personal/secret.md").read_text(), "private\n")

    def test_replace_backs_up_and_append_adds(self):
        r = run_apply_no_systemd(self.req("04-Knowledge/rust.md", "# Rust v2", "replace", "nathan@dm"))
        self.assertTrue(r["ok"], r)
        self.assertEqual((VAULT / "04-Knowledge/rust.md").read_text(), "# Rust v2\n")
        self.assertEqual(Path(r["backup"]).read_text(), "# Rust\nline 2\n")
        r = run_apply_no_systemd(self.req("04-Knowledge/rust.md", "- more", "append", "nathan@dm"))
        self.assertTrue(r["ok"], r)
        self.assertEqual((VAULT / "04-Knowledge/rust.md").read_text(), "# Rust v2\n- more\n")
        r = run_apply_no_systemd(self.req("04-Knowledge/missing.md", "x", "replace", "nathan@dm"))
        self.assertFalse(r["ok"])

    def test_symlink_out_of_vault_refused(self):
        outside = ROOT / "outside"
        outside.mkdir()
        (VAULT / "_inbox" / "link").symlink_to(outside)
        r = run_apply_no_systemd(self.req("_inbox/link/x.md"))
        self.assertFalse(r["ok"])
        self.assertIn("symlink", r["error"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_request_file_must_be_in_requests_folder(self):
        f = ROOT / "elsewhere.json"
        f.write_text(json.dumps(self.req("_inbox/a.md")))
        out = subprocess.run([sys.executable, str(HERE / "apply.py"), str(f)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertFalse((VAULT / "_inbox/a.md").exists())

    def test_waits_for_the_vault_lock(self):
        import fcntl
        applier.LOCK_WAIT_SECS = 1
        with (STATE / "vault.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(rules.Refused):
                applier.apply(self.req("_inbox/locked.md"))
        self.assertTrue(applier.apply(self.req("_inbox/locked.md"))["ok"])


class FreeFolders(Base):
    def test_free_folder_writes_without_asking(self):
        r = self.call(action="write", path="05-Brain-Dump/idea.md", content="# Idea")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("needs_confirmation", r)
        self.assertEqual((VAULT / "05-Brain-Dump/idea.md").read_text(), "# Idea\n")
        self.assertIn("VAULT-WRITE", self.audit())
        self.assertIn("free folder", self.audit())

    def test_free_folder_works_in_channels_too(self):
        self.env["HERMES_SESSION_CHAT_TYPE"] = "group"
        self.env["HERMES_SESSION_CHAT_NAME"] = "vault-queries"
        self.assertTrue(self.call(action="write", path="_inbox/x.md", content="x")["ok"])


class AskFolders(Base):
    def test_asks_then_writes_only_after_yes(self):
        r = self.call(action="write", path="04-Knowledge/go.md", content="# Go\nGoroutines are cheap.")
        self.assertTrue(r["needs_confirmation"])
        self.assertIn("create 04-Knowledge/go.md (2 lines)", r["ask_nathan"])
        self.assertIn("Goroutines are cheap", r["ask_nathan"])
        self.assertEqual(self.jobs, [])
        self.assertFalse((VAULT / "04-Knowledge/go.md").exists())

        # The model calling confirm on its own does nothing.
        r = self.call(action="confirm")
        self.assertFalse(r["ok"])
        self.assertIn("hasn't said yes", r["error"])
        self.assertFalse((VAULT / "04-Knowledge/go.md").exists())

        ctx = plugin._on_pre_llm_call(session_id="s1", user_message="Yes, go ahead")
        self.assertIn("said yes", ctx["context"])
        r = self.call(action="confirm")
        self.assertTrue(r["ok"], r)
        self.assertEqual((VAULT / "04-Knowledge/go.md").read_text(), "# Go\nGoroutines are cheap.\n")
        self.assertEqual(self.jobs[0]["approved_by"], "nathan@dm")
        self.assertIn("approved by nathan@dm", self.audit())
        self.assertEqual(plugin._pending, {})

    def test_replace_question_mentions_backup(self):
        r = self.call(action="write", path="04-Knowledge/rust.md", content="# Rust\nnew", how="replace")
        self.assertIn("2 lines now → 2 lines; the old note is backed up", r["ask_nathan"])

    def test_anything_but_a_plain_yes_cancels(self):
        for reply in ("no", "yes but change the title first", "wait", "what will it say?", "not yet", ""):
            plugin._pending.clear()
            self.call(action="write", path="04-Knowledge/go.md", content="# Go")
            ctx = plugin._on_pre_llm_call(session_id="s1", user_message=reply)
            self.assertIn("NOT written", ctx["context"], reply)
            self.assertFalse(self.call(action="confirm")["ok"], reply)
        self.assertFalse((VAULT / "04-Knowledge/go.md").exists())
        self.assertIn("declined", self.audit())

    def test_voice_and_phone_yes(self):
        for reply in ('"Yeah."', "ok do it", "[Voice note] yes please", "Yep!"):
            self.assertTrue(plugin.is_yes(reply), reply)
        self.env["HERMES_SESSION_PLATFORM"] = "api_server"
        r = self.call(action="write", path="02-Projects/plan.md", content="# Plan")
        self.assertTrue(r["needs_confirmation"])
        plugin._on_pre_llm_call(session_id="s1", user_message=[{"type": "text", "text": "yes"}])
        self.assertTrue(self.call(action="confirm")["ok"])
        self.assertEqual(self.jobs[0]["approved_by"], "nathan@api_server")

    def test_other_sessions_cant_approve(self):
        self.call(action="write", path="04-Knowledge/go.md", content="# Go")
        plugin._on_pre_llm_call(session_id="someone-else", user_message="yes")
        self.assertFalse(self.call(action="confirm")["ok"])

    def test_subagent_messages_dont_count(self):
        self.call(action="write", path="04-Knowledge/go.md", content="# Go")
        self.assertIsNone(plugin._on_pre_llm_call(session_id="s1", user_message="yes", parent_session_id="s0"))
        self.assertFalse(self.call(action="confirm")["ok"])

    def test_channels_and_cron_cant_ask(self):
        self.env["HERMES_SESSION_CHAT_TYPE"] = "group"
        self.env["HERMES_SESSION_CHAT_NAME"] = "general-chat"
        r = self.call(action="write", path="04-Knowledge/go.md", content="# Go")
        self.assertFalse(r["ok"])
        self.assertIn("DMs or the phone app", r["error"])
        self.assertEqual(plugin._pending, {})
        self.env["HERMES_CRON_SESSION"] = "1"
        self.assertFalse(self.call(action="write", path="_inbox/x.md", content="x")["ok"])

    def test_request_expires(self):
        self.call(action="write", path="04-Knowledge/go.md", content="# Go")
        plugin._pending["s1"]["created"] -= plugin._CONFIRM_SECS + 1
        ctx = plugin._on_pre_llm_call(session_id="s1", user_message="yes")
        self.assertIn("expired", ctx["context"])
        self.assertFalse(self.call(action="confirm")["ok"])

    def test_never_folders_refused_up_front(self):
        for path in ("01-Personal/x.md", "07-Security/x.md", "AGENTS.md", "/workspace/vault/.obsidian/x.md"):
            r = self.call(action="write", path=path, content="x")
            self.assertFalse(r["ok"], path)
            self.assertTrue(r.get("refused"), path)
        self.assertEqual(plugin._pending, {})
        self.assertEqual(self.jobs, [])

    def test_status_and_command(self):
        s = self.call(action="status")
        self.assertEqual(s["free_folders"], ["_inbox", "05-Brain-Dump", "10-Wiki"])
        self.call(action="write", path="05-Brain-Dump/a.md", content="a")
        out = plugin._cmd_vault_writes()
        self.assertIn("Free (no asking): _inbox, 05-Brain-Dump, 10-Wiki", out)
        self.assertIn("created 05-Brain-Dump/a.md", out)


@unittest.skipUnless(os.environ.get("PROXAI_TEST_SYSTEMD") == "1", "set PROXAI_TEST_SYSTEMD=1")
class RealSystemdJob(unittest.TestCase):
    """The real path: systemd-run --user with the plugin's sandbox settings."""

    def setUp(self):
        fresh_vault()

    def test_sandboxed_job_writes_and_cant_see_hidden_folders(self):
        spec2 = importlib.util.spec_from_file_location("vw_real", HERE / "__init__.py",
                                                       submodule_search_locations=[str(HERE)])
        real = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(real)
        req = {"id": os.urandom(6).hex(), "path": "04-Knowledge/systemd.md", "content": "# via systemd",
               "how": "create", "approved_by": "nathan@test"}
        t = time.monotonic()
        r = real._run_apply(req)
        self.assertTrue(r["ok"], r)
        self.assertEqual((VAULT / "04-Knowledge/systemd.md").read_text(), "# via systemd\n")
        print(f"\n  sandboxed write job took {time.monotonic() - t:.2f}s")
        # Same sandbox, but try to read the hidden folder and reach the network.
        probe = subprocess.run(
            ["systemd-run", "--user", "--wait", "--collect", "--quiet", "--pipe", *real._sandbox_props(),
             "/bin/sh", "-c", f"cat '{VAULT}/01-Personal/secret.md' && echo LEAK; "
                              f"ls '{VAULT}/07-Security' && echo LEAK2; ls '{Path.home()}/.ssh' && echo LEAK3; "
                              "echo done"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("done", probe.stdout, probe.stderr)
        self.assertNotIn("LEAK", probe.stdout)
        self.assertNotIn("private", probe.stdout)


class PointsAtVaultWrite(Base):
    """The local model can't see vault_write (hidden behind tool_search), so it tries write_file."""
    def block(self, path, tool="write_file"):
        return plugin._on_pre_tool_call(tool_name=tool, args={"path": path, "content": "x"})

    def test_write_file_to_read_only_folder_is_redirected(self):
        r = self.block("/workspace/vault/05-Brain-Dump/cron-idea.md")
        self.assertEqual(r["action"], "block")
        self.assertIn('"name": "vault_write"', r["message"])
        self.assertIn('"path": "05-Brain-Dump/cron-idea.md"', r["message"])
        self.assertIn("written right away", r["message"])
        r = self.block("/workspace/vault/04-Knowledge/go.md", tool="patch")
        self.assertIn("needs Nathan's permission", r["message"])

    def test_writable_folders_other_tools_and_paths_pass(self):
        self.assertIsNone(self.block("/workspace/vault/_inbox/x.md"))
        self.assertIsNone(self.block("/workspace/vault/10-Wiki/x.md"))
        self.assertIsNone(self.block("/workspace/proxai/notes.md"))
        self.assertIsNone(plugin._on_pre_tool_call(tool_name="read_file",
                                                   args={"path": "/workspace/vault/04-Knowledge/rust.md"}))

    def test_never_and_channel_cases(self):
        self.assertIn("never", self.block("/workspace/vault/07-Security/x.md")["message"])
        self.env["HERMES_SESSION_CHAT_TYPE"] = "group"
        self.env["HERMES_SESSION_CHAT_NAME"] = "general-chat"
        self.assertIn("_inbox", self.block("/workspace/vault/04-Knowledge/go.md")["message"])
        self.assertIn("vault_write", self.block("/workspace/vault/05-Brain-Dump/x.md")["message"])

    def test_save_requests_get_a_hint(self):
        for msg in ("add that to brain dump as a feature to fix", "save this to my vault",
                    "Put a note in the wiki about rust lifetimes"):
            r = plugin._on_pre_llm_call(session_id="s1", user_message=msg)
            self.assertIn("vault_write", r["context"], msg)
        for msg in ("what's in my brain dump?", "yes", "save the file in the repo"):
            self.assertIsNone(plugin._on_pre_llm_call(session_id="s1", user_message=msg), msg)
        self.env["HERMES_SESSION_CHAT_TYPE"] = "group"
        self.assertIsNone(plugin._on_pre_llm_call(session_id="s1", user_message="save this to my vault"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
