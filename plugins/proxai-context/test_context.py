"""Run: python3 plugins/proxai-context/test_context.py"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(tempfile.mkdtemp(prefix="context-test-", dir=os.environ.get("TMPDIR")))
os.environ["PROXAI_CONTEXT_DIR"] = str(STATE)
spec = importlib.util.spec_from_file_location("proxai_context_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_context_plugin"] = plugin
spec.loader.exec_module(plugin)


def saved() -> dict:
    return json.loads((STATE / "sessions.json").read_text())


class Context(unittest.TestCase):
    def setUp(self):
        (STATE / "sessions.json").unlink(missing_ok=True)

    def call(self, sid, prompt, platform="api_server", n=1):
        plugin._on_post_api_request(session_id=sid, platform=platform, model="qwen3.5:35b-a3b",
                                    usage={"prompt_tokens": prompt, "input_tokens": 1}, context_length=65536,
                                    api_call_count=n)

    def test_keeps_the_latest_call_not_the_sum(self):
        for n, p in enumerate((32396, 32662, 33100), 1):
            self.call("android-a", p, n=n)
        d = saved()
        self.assertEqual(d["sessions"]["android-a"]["prompt_tokens"], 33100)
        self.assertEqual(d["sessions"]["android-a"]["context_length"], 65536)
        self.assertEqual(d["latest"]["call"], 3)

    def test_only_phone_sessions_and_real_numbers(self):
        self.call("discord-x", 5000, platform="discord")
        self.call("android-b", 0)
        plugin._on_post_api_request(session_id="android-c", platform="api_server", usage=None)
        self.assertFalse((STATE / "sessions.json").exists())

    def test_keeps_30_sessions(self):
        for i in range(35):
            plugin.record(f"s{i:02d}", 1000 + i, 65536, "m", 1)
        d = saved()
        self.assertEqual(len(d["sessions"]), 30)
        self.assertEqual(d["latest"]["session"], "s34")


if __name__ == "__main__":
    unittest.main(verbosity=2)
