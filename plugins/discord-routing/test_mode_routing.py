"""Phase 10: the server/personal mode can swap a tier's model (mode.json "hermes" section).

Run: python3 plugins/discord-routing/test_mode_routing.py
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_captures import load_plugin

CONFIG = {
    "default_tier": "tier2",
    "channels": {"brain-dump": "tier1", "general-chat": "tier2"},
    "tiers": {
        "tier1": {"label": "Ollama / qwen3.5:9b", "model": "qwen3.5:9b", "provider": "ollama",
                  "base_url": "http://localhost:11434/v1", "api_mode": "chat_completions"},
        "tier2": {"label": "Sonnet", "model": "claude-sonnet-5-5", "provider": "anthropic"},
    },
}


class TestModeRouting(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.tmp = Path(tempfile.mkdtemp())
        self.plugin._MODE_FILE = self.tmp / "mode.json"

    def write_mode(self, hermes):
        self.plugin._MODE_FILE.write_text(json.dumps({"mode": "x", "hermes": hermes}))

    def test_no_mode_file_uses_routing_json(self):
        r = self.plugin._routing_for("brain-dump", CONFIG)
        self.assertEqual(r["model"], "qwen3.5:9b")

    def test_server_mode_swaps_tier1_model_only(self):
        self.write_mode({"voice": True, "tier1": {"model": "big:20b", "label": "Ollama / big (server)"}})
        r = self.plugin._routing_for("brain-dump", CONFIG)
        self.assertEqual(r["model"], "big:20b")
        self.assertEqual(r["label"], "Ollama / big (server)")
        self.assertEqual(r["base_url"], "http://localhost:11434/v1")     # rest of the tier kept
        self.assertEqual(self.plugin._routing_for("general-chat", CONFIG)["model"], "claude-sonnet-5-5")

    def test_personal_mode_without_overrides(self):
        self.write_mode({"voice": False})
        self.assertEqual(self.plugin._routing_for("brain-dump", CONFIG)["model"], "qwen3.5:9b")

    def test_broken_mode_file_is_ignored(self):
        self.plugin._MODE_FILE.write_text("{not json")
        self.assertEqual(self.plugin._routing_for("brain-dump", CONFIG)["model"], "qwen3.5:9b")


if __name__ == "__main__":
    unittest.main(verbosity=1)
