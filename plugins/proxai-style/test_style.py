"""Run: python3 plugins/proxai-style/test_style.py"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("proxai_style_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_style_plugin"] = plugin
spec.loader.exec_module(plugin)


class Style(unittest.TestCase):
    def test_how_to_questions(self):
        for msg in ("how do I set up a cron job", "How to add a deploy key?", "hey proxai, how can I check my GPU temp",
                    "what's the best way to back up the vault", "\"How do I restart the gateway?\"",
                    "walk me through merging a branch", [{"type": "text", "text": "how would I install gh"}]):
            r = plugin._on_pre_llm_call(user_message=msg)
            self.assertIsNotNone(r, msg)
            self.assertIn("Explain it briefly", r["context"])

    def test_requests_to_act_pass(self):
        for msg in ("set up a cron job for 2am", "delete pomodoro app", "how do I set it up? go ahead and do it",
                    "what is my GPU temp", "can you do it for me", "show me how the build went"):
            self.assertIsNone(plugin._on_pre_llm_call(user_message=msg), msg)
        self.assertIsNone(plugin._on_pre_llm_call(user_message="how do I x", parent_session_id="p"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
