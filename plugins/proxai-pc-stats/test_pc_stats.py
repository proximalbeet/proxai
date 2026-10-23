"""Tests for proxai-pc-stats against a fake thermal-watch folder.

Run: python3 plugins/proxai-pc-stats/test_pc_stats.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(tempfile.mkdtemp(prefix="pc-stats-test-", dir=os.environ.get("TMPDIR")))
os.environ["PROXAI_THERMAL_DIR"] = str(STATE)
spec = importlib.util.spec_from_file_location("proxai_pc_stats_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_pc_stats_plugin"] = plugin
spec.loader.exec_module(plugin)

NOW = datetime(2026, 10, 23, 11, 52, 30)
SAMPLE = {"time": "2026-10-23T11:52:16", "gpu_c": 41.0, "gpu_w": 10.04, "gpu_use": 0.0, "vram_mib": 15376.0,
          "fan_pct": 0.0, "sm_mhz": 180.0, "throttle": "0x400", "cpu_pkg_c": 42.0, "cpu_core_max_c": 44.0,
          "cpu_use": 0.3, "ram_used_mb": 8066, "ram_avail_mb": 23699, "swap_mb": 5168, "vram_total_mib": 16303.0,
          "limits": {"gpu_warn_c": 75, "gpu_crit_c": 82, "gpu_power_warn_w": 250, "cpu_warn_c": 85,
                     "cpu_crit_c": 95, "ram_warn_mb": 2048}}


class PcStats(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(STATE, ignore_errors=True)
        STATE.mkdir(parents=True)
        (STATE / "latest.json").write_text(json.dumps(SAMPLE))
        alerts = [{"id": 1, "time": "2026-10-21T09:00:00", "level": "ALERT", "kind": "gpu", "msg": "old one"},
                  {"id": 2, "time": "2026-10-22T23:49:53", "level": "ALERT", "kind": "cpu_rise",
                   "msg": "CPU temp jumped 55->82C in 5 s (use 23%)"},
                  {"id": 3, "time": "2026-10-22T23:50:34", "level": "ALERT", "kind": "swap",
                   "msg": "swap grew by 1049 MB (now 4056 MB)"}]
        (STATE / "alerts.jsonl").write_text("\n".join(json.dumps(a) for a in alerts) + "\nnot json\n")

    def test_summary_has_the_numbers(self):
        s = plugin.summary(now=NOW)
        for part in ("GPU: 41 °C, 10 W, 0% busy", "VRAM 15.0/15.9 GB", "CPU: 42 °C (hottest core 44 °C)",
                     "RAM: 7.9 GB used, 23.1 GB free", "swap 5.0 GB", "GPU 75/82 °C",
                     "Alerts in the last 24 h: 2", "23:50 swap grew"):
            self.assertIn(part, s)
        self.assertNotIn("old one", s)
        self.assertNotIn("WARNING", s)
        self.assertNotIn("throttling", s)   # 0x400 isn't a heat bit

    def test_stale_missing_and_throttle(self):
        later = datetime(2026, 10, 23, 12, 30)
        self.assertIn("WARNING: these readings are 37 min old", plugin.summary(now=later))
        (STATE / "latest.json").write_text(json.dumps({**SAMPLE, "throttle": "0x40", "gpu_c": None}))
        s = plugin.summary(now=NOW)
        self.assertIn("thermal throttling", s)
        self.assertIn("GPU: ? °C", s)
        (STATE / "latest.json").unlink()
        self.assertIn("no readings", plugin.summary(now=NOW))
        self.assertFalse(json.loads(plugin._handle({}))["ok"])

    def test_hook_answers_pc_questions_only(self):
        for msg in ("What is my GPU temp", "how hot is my pc", "what are my specs?", "how's the pc holding up",
                    "check temps please", "is the CPU fan ok", [{"type": "text", "text": "gpu usage?"}]):
            r = plugin._on_pre_llm_call(user_message=msg)
            self.assertIsNotNone(r, msg)
            self.assertIn("GPU:", r["context"])
            self.assertIn("don't probe /sys", r["context"])
        for msg in ("make me a pomodoro app", "what's the weather", "temporary fix for the build",
                    "add that to brain dump", "yes"):
            self.assertIsNone(plugin._on_pre_llm_call(user_message=msg), msg)
        self.assertIsNone(plugin._on_pre_llm_call(user_message="gpu temp?", parent_session_id="p"))

    def test_tool_and_command(self):
        r = json.loads(plugin._handle({"hours": "48"}))
        self.assertTrue(r["ok"])
        self.assertIn("GPU:", r["summary"])
        self.assertIn("in the last 48 h", r["summary"])
        self.assertTrue(plugin._cmd_pc().startswith("🌡️ PC right now"))

    def test_registers(self):
        seen = []

        class Ctx:
            def register_tool(self, **kw): seen.append(("tool", kw["name"]))
            def register_command(self, name, *_a, **_k): seen.append(("cmd", name))
            def register_hook(self, name, _fn): seen.append(("hook", name))
        plugin.register(Ctx())
        self.assertEqual(seen, [("tool", "pc_stats"), ("cmd", "pc"), ("hook", "pre_llm_call")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
