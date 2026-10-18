"""Tests for vault_sync.py against throwaway repos: a bare "GitHub" plus laptop and desktop clones.

Run: python3 -m unittest vault-sync/test_vault_sync.py  (from the profile root) or
     python3 test_vault_sync.py  (from this folder)
"""
from __future__ import annotations

import fcntl
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
ENV.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


def write(path: Path, text: str, age: float = 60) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    old = time.time() - age  # look "quiet" unless the test says otherwise
    os.utime(path, (old, old))


class VaultSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vault-sync-test-", dir=os.environ.get("TMPDIR")))
        self.remote, self.laptop, self.desktop = (self.tmp / n for n in ("remote.git", "laptop", "desktop"))
        self.state = self.tmp / "state"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], env=ENV, check=True)
        seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], env=ENV, check=True)
        write(seed / "04-Knowledge/a.md", "line 1\nline 2\nline 3\n")
        write(seed / "01-Personal/TODO.md", "- one\n")
        write(seed / ".obsidian/workspace.json", '{"layout": 0}\n')
        write(seed / ".obsidian/app.json", "{}\n")
        sh(seed, "add", "-A"); sh(seed, "commit", "-qm", "seed")
        sh(seed, "push", "-q", str(self.remote), "main")
        for clone in (self.laptop, self.desktop):
            subprocess.run(["git", "clone", "-q", str(self.remote), str(clone)], env=ENV, check=True)
        os.environ.update({"PROXAI_VAULT": str(self.desktop), "PROXAI_STATE": str(self.state),
                           "PROXAI_VAULT_REMOTE": str(self.remote), "PROXAI_VAULT_LOCK_WAIT": "1"})
        import vault_sync
        self.vs = importlib.reload(vault_sync)

    def laptop_push(self, rel: str, text: str):
        write(self.laptop / rel, text)
        sh(self.laptop, "add", "-A"); sh(self.laptop, "commit", "-qm", "laptop edit")
        sh(self.laptop, "push", "-q", "origin", "main")

    def state_json(self) -> dict:
        return json.loads((self.state / "vault-sync.json").read_text())

    # ------------------------------------------------------------------ tests

    def test_different_notes_sync_both_ways(self):
        self.laptop_push("04-Knowledge/laptop.md", "from laptop\n")
        write(self.desktop / "_inbox/desktop.md", "from desktop\n")
        self.assertEqual(self.vs.run(), 0)
        s = self.state_json()
        self.assertEqual(s["result"], "ok")
        self.assertEqual((s["committed"], s["pulled"], s["pushed"]), (1, 1, 1))
        self.assertTrue((self.desktop / "04-Knowledge/laptop.md").exists())
        sh(self.laptop, "pull", "-q", "--rebase")
        self.assertTrue((self.laptop / "_inbox/desktop.md").exists())
        self.assertEqual(sh(self.desktop, "rev-parse", "HEAD"), sh(self.remote, "rev-parse", "main"))
        # history stays linear (rebase, never a merge commit)
        self.assertEqual(sh(self.desktop, "rev-list", "--merges", "--count", "HEAD"), "0")

    def test_same_note_conflict_stops_cleanly(self):
        self.laptop_push("04-Knowledge/a.md", "line 1\nLAPTOP\nline 3\n")
        write(self.desktop / "04-Knowledge/a.md", "line 1\nDESKTOP\nline 3\n")
        remote_before = sh(self.remote, "rev-parse", "main")
        self.assertEqual(self.vs.run(), 1)
        s = self.state_json()
        self.assertEqual(s["result"], "conflict")
        self.assertEqual(s["conflict_files"], ["04-Knowledge/a.md"])
        # files untouched, no rebase left behind, nothing pushed
        self.assertEqual((self.desktop / "04-Knowledge/a.md").read_text(), "line 1\nDESKTOP\nline 3\n")
        self.assertFalse((self.desktop / ".git/rebase-merge").exists())
        self.assertEqual(sh(self.remote, "rev-parse", "main"), remote_before)
        # next run: same alert key and "since", so Hermes alerts once
        key, since = s["alert_key"], s["since"]
        time.sleep(1.1)
        self.assertEqual(self.vs.run(), 1)
        s2 = self.state_json()
        self.assertEqual((s2["alert_key"], s2["since"]), (key, since))
        # Fix by hand (take laptop's line + desktop's), then the next run syncs
        sh(self.desktop, "fetch", "-q", "origin")
        sh(self.desktop, "reset", "-q", "--hard", "origin/main")
        write(self.desktop / "04-Knowledge/a.md", "line 1\nLAPTOP\nDESKTOP\nline 3\n")
        self.assertEqual(self.vs.run(), 0)
        self.assertEqual(self.state_json()["result"], "ok")

    def test_private_paths_are_masked(self):
        self.laptop_push("01-Personal/TODO.md", "- laptop\n")
        write(self.desktop / "01-Personal/TODO.md", "- desktop\n")
        self.assertEqual(self.vs.run(), 1)
        s = self.state_json()
        self.assertEqual(s["conflict_files"], ["01-Personal/<hidden>"])
        self.assertNotIn("TODO", json.dumps(s))
        # the host log keeps the real path for Nathan
        self.assertIn("01-Personal/TODO.md", (self.state / "vault-sync.log").read_text())

    def test_skips_while_a_note_is_being_written(self):
        write(self.desktop / "_inbox/new.md", "half written", age=0)
        self.assertEqual(self.vs.run(), 0)
        s = self.state_json()
        self.assertEqual(s["result"], "skipped")
        self.assertEqual(sh(self.desktop, "rev-list", "--count", "HEAD"), "1")

    def test_waits_for_the_refiler_lock(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "vault.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.vs.run(), 0)
        self.assertEqual(self.state_json()["result"], "locked")

    def test_refuses_mid_rebase_repo(self):
        (self.desktop / ".git/rebase-merge").mkdir()
        self.assertEqual(self.vs.run(), 1)
        self.assertEqual(self.state_json()["result"], "blocked")

    def test_dry_run_changes_nothing(self):
        write(self.desktop / "_inbox/x.md", "x\n")
        self.assertEqual(self.vs.run(dry_run=True), 0)
        self.assertEqual(sh(self.desktop, "status", "--porcelain"), "?? _inbox/")
        self.assertFalse((self.state / "vault-sync.json").exists())

    def test_oversized_file_left_out(self):
        self.vs.MAX_FILE_BYTES = 10
        write(self.desktop / "_inbox/big.bin", "x" * 100)
        write(self.desktop / "_inbox/small.md", "ok\n")
        self.assertEqual(self.vs.run(), 0)
        s = self.state_json()
        self.assertEqual(s["committed"], 1)
        self.assertIn("big.bin", s["warnings"][0])
        self.assertNotIn("_inbox/big.bin", sh(self.remote, "ls-tree", "-r", "--name-only", "main"))

    def test_prepare_untracks_workspace_without_conflict(self):
        # both sides changed workspace.json: a plain pull would conflict
        self.laptop_push(".obsidian/workspace.json", '{"layout": "laptop"}\n')
        write(self.desktop / ".obsidian/workspace.json", '{"layout": "desktop"}\n')
        write(self.desktop / "_inbox/n.md", "note\n")
        self.assertEqual(self.vs.prepare(apply=True), 0, self.state_json())
        files = sh(self.remote, "ls-tree", "-r", "--name-only", "main").split()
        self.assertIn(".gitignore", files)
        self.assertIn("_inbox/n.md", files)
        self.assertNotIn(".obsidian/workspace.json", files)
        self.assertIn(".obsidian/app.json", files)  # shared settings stay synced
        # the desktop keeps its own layout, now ignored
        self.assertEqual((self.desktop / ".obsidian/workspace.json").read_text(), '{"layout": "desktop"}\n')
        self.assertEqual(sh(self.desktop, "status", "--porcelain"), "")
        # later layout changes never reach git
        write(self.desktop / ".obsidian/workspace.json", '{"layout": "moved"}\n')
        self.assertEqual(self.vs.run(), 0)
        self.assertEqual(self.state_json()["message"], "already in sync")

    def test_never_force_pushes_over_remote_history(self):
        # laptop pushes after our fetch: simulate by pushing between commit and push
        write(self.desktop / "_inbox/d.md", "d\n")
        real_fetch = self.vs.fetch

        def fetch_then_laptop_pushes():
            real_fetch()
            self.laptop_push("04-Knowledge/late.md", "late\n")
        self.vs.fetch = fetch_then_laptop_pushes
        self.assertEqual(self.vs.run(), 0)
        self.assertEqual(self.state_json()["result"], "retry")
        self.assertIn("04-Knowledge/late.md", sh(self.remote, "ls-tree", "-r", "--name-only", "main"))
        self.vs.fetch = real_fetch
        self.assertEqual(self.vs.run(), 0)
        files = sh(self.remote, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("04-Knowledge/late.md", files)
        self.assertIn("_inbox/d.md", files)


if __name__ == "__main__":
    unittest.main()
