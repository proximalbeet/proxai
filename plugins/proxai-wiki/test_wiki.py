"""Tests for proxai-wiki: real git and the real promote step against a throwaway vault; only Claude
Code is faked (it writes a page per note). The real vault is never touched.

Run: python3 plugins/proxai-wiki/test_wiki.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(tempfile.mkdtemp(prefix="wiki-test-", dir=os.environ.get("TMPDIR")))
VAULT, STATE, WORK = ROOT / "vault", ROOT / "state", ROOT / "work"
os.environ.update(PROXAI_VAULT=str(VAULT), PROXAI_STATE_DIR=str(STATE), PROXAI_WIKI_WORK=str(WORK),
                  PROXAI_WIKI_NIGHTLY="off")
spec = importlib.util.spec_from_file_location("proxai_wiki_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_wiki_plugin"] = plugin
spec.loader.exec_module(plugin)
pspec = importlib.util.spec_from_file_location("proxai_wiki_promote", HERE / "promote.py")
promote = importlib.util.module_from_spec(pspec)
pspec.loader.exec_module(promote)

plugin.WORK = str(WORK)                       # the "sandbox" is this machine in the test
plugin.VAULT_SANDBOX = str(VAULT)


def local_shell(command, task_id=None, timeout=120):
    r = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout)
    return {"output": r.stdout + r.stderr, "exit_code": r.returncode}


class FakeClaude:
    """Writes concepts/<note-name>.md for each note, updates index.md and the digest."""

    def __init__(self):
        self.calls, self.ok = [], True

    def __call__(self, notes, policy, task_id=None):
        self.calls.append([n for n, _ in notes])
        if not self.ok:
            (WORK / "concepts" / "half-written.md").write_text("oops")
            return {"ok": False, "error": "timed out", "model": "sonnet"}
        (WORK / ".proxai").mkdir(exist_ok=True)
        digest = []
        for rel, _ in notes:
            name = Path(rel).stem.lower()
            (WORK / "concepts").mkdir(exist_ok=True)
            (WORK / "concepts" / f"{name}.md").write_text(f"---\ntitle: {name}\nsources: [{rel}]\n---\n# {name}\n")
            digest.append(f"- concepts/{name} (new): from {rel}")
        with (WORK / "index.md").open("a") as f:
            f.write("".join(f"- [[concepts/{Path(r).stem.lower()}]]\n" for r, _ in notes))
        (WORK / ".proxai" / "digest.md").write_text("\n".join(digest))
        return {"ok": True, "model": "sonnet"}


def note(rel, text="# note\nsome knowledge\n"):
    p = VAULT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class Wiki(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(ROOT, ignore_errors=True)
        for d in ("10-Wiki/concepts", "03-Courses/Data Structures", "05-Brain-Dump", "01-Personal", "07-Security"):
            (VAULT / d).mkdir(parents=True, exist_ok=True)
        (VAULT / "10-Wiki" / "index.md").write_text("# Wiki Index\n")
        (VAULT / "10-Wiki" / "concepts" / "c-pointers.md").write_text("# C pointers\n")
        note("01-Personal/diary.md", "secret")
        note("07-Security/keys.md", "secret")
        STATE.mkdir(parents=True)
        plugin._shell = local_shell
        self.claude = FakeClaude()
        plugin.run_claude = self.claude
        plugin._run_promote = lambda req: self._promote(req)
        plugin._audit = lambda text: None
        plugin._asked.clear()
        plugin._job = None
        plugin._session = lambda name: {"HERMES_SESSION_PLATFORM": "api_server"}.get(name, "")

    def _promote(self, req):
        try:
            return promote.apply(req)
        except promote.Refused as exc:
            return {"ok": False, "error": str(exc)}

    def wiki(self, rel):
        p = VAULT / "10-Wiki" / rel
        return p.read_text() if p.exists() else None

    def test_auto_notes_go_straight_in_review_notes_wait(self):
        note("03-Courses/Data Structures/Hash-Tables.md")
        note("05-Brain-Dump/2026-10-23-idea.md", "# cron idea\n")
        r = plugin.run_update("request")
        self.assertTrue(r["ok"], r)
        self.assertIn("# hash-tables", self.wiki("concepts/hash-tables.md"))
        self.assertIsNone(self.wiki("concepts/2026-10-23-idea.md"))        # waits for a yes
        self.assertIsNone(self.wiki(".proxai/digest.md"))
        self.assertIn("# C pointers", self.wiki("concepts/c-pointers.md"))  # untouched
        st = plugin.load_state()
        self.assertEqual(st["review"]["notes"][0][0], "05-Brain-Dump/2026-10-23-idea.md")
        self.assertIn("2026-10-23-idea (new)", st["review"]["digest"])
        # Nothing ever read from 01-Personal / 07-Security.
        self.assertFalse(any("Personal" in n or "Security" in n for c in self.claude.calls for n in c))
        # Second run: nothing new, Claude not called again.
        self.assertEqual(plugin.run_update("request").get("note"), "nothing new")
        self.assertEqual(len(self.claude.calls), 2)

    def test_approve_puts_the_review_in(self):
        note("05-Brain-Dump/idea.md")
        plugin.run_update("nightly")
        note("03-Courses/Data Structures/Graphs.md")     # main moves on before the approval
        plugin.run_update("request")
        r = plugin.approve()
        self.assertTrue(r["ok"], r)
        self.assertIsNotNone(self.wiki("concepts/idea.md"))
        self.assertIsNotNone(self.wiki("concepts/graphs.md"))
        self.assertIsNone(plugin.load_state()["review"])
        self.assertIn("[[concepts/idea]]", self.wiki("index.md"))

    def test_approve_redoes_notes_when_a_page_changed_on_both_sides(self):
        note("05-Brain-Dump/sorting.md")
        plugin.run_update("nightly")
        note("03-Courses/Data Structures/Sorting.md")    # same page, concepts/sorting.md, on main
        plugin.run_update("request")
        r = plugin.approve()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.claude.calls[-1], ["05-Brain-Dump/sorting.md"])   # written again on top
        self.assertIsNone(plugin.load_state()["review"])
        attrs = (WORK / ".gitattributes").read_text()
        self.assertEqual(attrs, "index.md merge=union\nhot.md merge=union\nlog.md merge=union\n")

    def test_reject_drops_it_until_the_note_changes(self):
        note("05-Brain-Dump/idea.md")
        plugin.run_update("nightly")
        self.assertTrue(plugin.reject()["ok"])
        self.assertIsNone(self.wiki("concepts/idea.md"))
        self.assertEqual(plugin.run_update("request").get("note"), "nothing new")
        note("05-Brain-Dump/idea.md", "# idea, edited\n")
        plugin.run_update("request")
        self.assertIsNotNone(plugin.load_state()["review"])

    def test_undo_reverts_the_last_update(self):
        note("03-Courses/Data Structures/Heaps.md")
        plugin.run_update("request")
        self.assertIsNotNone(self.wiki("concepts/heaps.md"))
        r = plugin.undo()
        self.assertTrue(r["ok"], r)
        self.assertIsNone(self.wiki("concepts/heaps.md"))
        self.assertEqual(self.wiki("index.md"), "# Wiki Index\n")
        self.assertEqual(plugin.load_state()["promotions"], [])
        self.assertEqual(plugin.run_update("request").get("note"), "nothing new")   # not re-added
        backups = list((STATE / "wiki" / "backups").rglob("*.md"))
        self.assertTrue(backups)

    def test_wiki_no_and_failed_runs(self):
        note("04-Knowledge/private-ish.md", "---\ntitle: x\nwiki: no\n---\nkeep out\n")
        note("03-Courses/Data Structures/Tries.md")
        self.claude.ok = False
        r = plugin.run_update("request")
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["auto_error"])
        self.assertIsNone(self.wiki("concepts/half-written.md"))
        self.claude.ok = True
        plugin.run_update("request")
        self.assertIsNotNone(self.wiki("concepts/tries.md"))
        self.assertNotIn("04-Knowledge/private-ish.md", [n for c in self.claude.calls for n in c])

    def test_hand_edits_in_the_vault_are_kept(self):
        note("03-Courses/Data Structures/Stacks.md")
        plugin.run_update("request")
        (VAULT / "10-Wiki" / "concepts" / "stacks.md").write_text("# stacks, edited by Nathan\n")
        note("03-Courses/Data Structures/Queues.md")
        plugin.run_update("request")
        self.assertEqual(self.wiki("concepts/stacks.md"), "# stacks, edited by Nathan\n")
        self.assertIsNotNone(self.wiki("concepts/queues.md"))

    def test_promote_refuses_bad_paths(self):
        for bad in ("../01-Personal/x.md", ".git/config.md", "/etc/x.md", "concepts/x.sh"):
            with self.assertRaises(promote.Refused, msg=bad):
                promote.clean(bad)

    def test_chat_triggers(self):
        calls = []
        plugin._run_in_background = lambda fn, label, *a: calls.append(label)
        say = lambda t, sid="s1": plugin._on_pre_llm_call(session_id=sid, user_message=t)
        self.assertIn("ALREADY started", say("update the wiki")["context"])
        self.assertIn("hasn't run yet", say("wiki status")["context"])
        self.assertIn("No wiki update is waiting", say("approve the wiki update")["context"])
        self.assertIn("ALREADY started", say("undo the last wiki update")["context"])
        self.assertEqual(calls, ["update", "undo"])
        self.assertIsNone(say("how do I update the wiki?"))
        self.assertIsNone(say("what's my gpu temp"))
        plugin._session = lambda name: {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "group"}.get(name, "")
        self.assertIsNone(say("update the wiki"))

    def test_waiting_update_is_mentioned_once_then_yes_approves(self):
        note("05-Brain-Dump/idea.md")
        plugin.run_update("nightly")
        calls = []
        plugin._run_in_background = lambda fn, label, *a: calls.append(label)
        r = plugin._on_pre_llm_call(session_id="s1", user_message="good morning")
        self.assertIn("wiki update is waiting", r["context"])
        self.assertIsNone(plugin._on_pre_llm_call(session_id="s2", user_message="hi"))   # only once
        plugin._on_pre_llm_call(session_id="s1", user_message="yes")
        self.assertEqual(calls, ["approve"])

    def test_nightly_window(self):
        import datetime as dt
        pol = plugin.load_policy()
        self.assertTrue(plugin._nightly_due(dt.datetime(2026, 10, 24, 3, 31), pol, {}))
        self.assertFalse(plugin._nightly_due(dt.datetime(2026, 10, 24, 3, 31), pol, {"last_nightly": "2026-10-24"}))
        self.assertFalse(plugin._nightly_due(dt.datetime(2026, 10, 24, 14, 0), pol, {}))


class RealSystemdJob(unittest.TestCase):
    """PROXAI_TEST_SYSTEMD=1: promote.py as the real sandboxed systemd job, on a throwaway vault."""

    def test_job_writes_only_10_wiki(self):
        if os.environ.get("PROXAI_TEST_SYSTEMD") != "1":
            self.skipTest("set PROXAI_TEST_SYSTEMD=1")
        shutil.rmtree(ROOT, ignore_errors=True)
        for d in ("10-Wiki", "01-Personal", "04-Knowledge"):
            (VAULT / d).mkdir(parents=True)
        (VAULT / "01-Personal" / "diary.md").write_text("secret")
        (WORK / "concepts").mkdir(parents=True)
        (WORK / "concepts" / "x.md").write_text("# x\n")
        STATE.mkdir(parents=True)
        r = plugin._run_promote({"id": "t1", "changes": [["A", "concepts/x.md"]]})
        self.assertTrue(r["ok"], r)
        self.assertEqual((VAULT / "10-Wiki" / "concepts" / "x.md").read_text(), "# x\n")
        r = plugin._run_promote({"id": "t2", "changes": [["A", "../04-Knowledge/x.md"]]})
        self.assertFalse(r["ok"])
        self.assertFalse((VAULT / "04-Knowledge" / "x.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
