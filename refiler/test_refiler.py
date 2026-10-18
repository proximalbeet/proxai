"""Tests for refiler.py. Run: python3 -m unittest refiler/test_refiler.py -v"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TPL = """---
date: <% tp.date.now("YYYY-MM-DD") %>
day: <% tp.date.now("dddd") %>
---

# <% tp.date.now("dddd, MMMM D") %>

## ✅ Tasks
- [x] 
- [x] 

## 📝 Notes

---
*Yesterday's unfinished: [[<% tp.date.now("YYYY-MM-DD", -1) %>]]*"""


class RefilerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROXAI_STATE_DIR"] = str(root / "state")
        sys.path.insert(0, str(HERE))
        import importlib, refiler
        self.mod = importlib.reload(refiler)  # pick up the state dir
        self.vault = root / "vault"
        for d in ("_inbox", "_templates", "00-Daily", "01-Personal", "04-Knowledge",
                  "06-Focus-Logs", "_needs-review"):
            (self.vault / d).mkdir(parents=True)
        (self.vault / "_templates/daily-note.md").write_text(TPL)

    def tearDown(self):
        self.tmp.cleanup()

    def note(self, name, text, age=120):
        p = self.vault / "_inbox" / name
        p.write_text(text)
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def run_refiler(self, apply=True):
        self.mod.Refiler(self.vault, apply).run()

    def test_routes_by_type_and_area(self):
        self.note("k.md", "---\ntype: note\narea: knowledge\n---\nx\n")
        self.run_refiler()
        self.assertTrue((self.vault / "04-Knowledge/k.md").exists())
        self.assertIn("MOVE _inbox/k.md -> 04-Knowledge/k.md",
                      (Path(os.environ["PROXAI_STATE_DIR"]) / "refiler.log").read_text())

    def test_review_cases_never_personal(self):
        self.note("raw.md", "no frontmatter\n")
        self.note("personal.md", "---\ntype: note\narea: personal\n---\nx\n")
        self.note("weird.md", "---\ntype: widget\narea: knowledge\n---\nx\n")
        self.run_refiler()
        self.assertEqual(sorted(p.name for p in (self.vault / "_needs-review").iterdir()),
                         ["personal.md", "raw.md", "weird.md"])
        self.assertEqual(list((self.vault / "01-Personal").iterdir()), [])

    def test_dry_run_changes_nothing(self):
        self.note("k.md", "---\ntype: note\narea: knowledge\n---\nx\n")
        self.run_refiler(apply=False)
        self.assertTrue((self.vault / "_inbox/k.md").exists())
        self.assertFalse((Path(os.environ["PROXAI_STATE_DIR"]) / "refiler.log").exists())

    def test_skips_fresh_notes_and_subfolders(self):
        self.note("fresh.md", "---\ntype: note\narea: knowledge\n---\nx\n", age=0)
        (self.vault / "_inbox/_audit-test").mkdir()
        self.run_refiler()
        self.assertTrue((self.vault / "_inbox/fresh.md").exists())
        self.assertTrue((self.vault / "_inbox/_audit-test").is_dir())

    def test_daily_creates_note_from_template(self):
        self.note("g.md", "---\ntype: daily\ndate: 2026-12-01\n---\n1. **Ship** it\n2. Rest\n")
        self.run_refiler()
        daily = (self.vault / "00-Daily/2026-12-01.md").read_text()
        self.assertIn("# Tuesday, December 1", daily)
        self.assertIn("[[2026-11-30]]", daily)
        self.assertIn("## ✅ Tasks\n- [ ] Ship it\n- [ ] Rest\n\n## 📝 Notes", daily)
        self.assertNotIn("- [x] ", daily)
        self.assertFalse((self.vault / "_inbox/g.md").exists())
        backups = list((Path(os.environ["PROXAI_STATE_DIR"]) / "refiled").rglob("g.md"))
        self.assertEqual(len(backups), 1)

    def test_daily_appends_without_duplicates(self):
        (self.vault / "00-Daily/2026-10-15.md").write_text(
            "# Day\n\n## ✅ Tasks\n- [x] done already\n\n## 📝 Notes\nkeep me\n")
        self.note("a.md", "---\ntype: daily\ncreated: 2026-10-15\n---\n- done already\n- new one\n")
        self.run_refiler()
        daily = (self.vault / "00-Daily/2026-10-15.md").read_text()
        self.assertIn("## ✅ Tasks\n- [x] done already\n- [ ] new one\n\n## 📝 Notes\nkeep me", daily)

    def test_focus_log_gets_date_and_name(self):
        self.note("focus-session-1.md",
                  "---\ntype: focus-log\ntitle: Focus session — Write Report\n"
                  "start: 2026-10-15T18:26:41+00:00\n---\nbody\n")
        self.run_refiler()
        out = self.vault / "06-Focus-Logs/2026-10-15-1826-write-report.md"
        text = out.read_text()
        self.assertTrue(text.startswith('---\ndate: 2026-10-15\nday: "[[2026-10-15]]"\n'))

    def test_name_collision(self):
        (self.vault / "04-Knowledge/k.md").write_text("old")
        self.note("k.md", "---\ntype: note\narea: knowledge\n---\nnew\n")
        self.run_refiler()
        self.assertEqual((self.vault / "04-Knowledge/k.md").read_text(), "old")
        self.assertTrue((self.vault / "04-Knowledge/k-2.md").exists())


if __name__ == "__main__":
    unittest.main()
