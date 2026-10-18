"""Tests for plugin-written captures. Run: python3 -m unittest plugins/discord-routing/test_captures.py -v"""

import asyncio
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TZ = dt.timezone(dt.timedelta(hours=-4))


def load_plugin():
    """Load the plugin folder as a package, the way Hermes does."""
    for name in [m for m in sys.modules if m.startswith("dr_test_pkg")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "dr_test_pkg", HERE / "__init__.py", submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dr_test_pkg"] = mod
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        (self.vault / "_inbox").mkdir(parents=True)
        (self.vault / "05-Brain-Dump").mkdir()
        self.state = root / "state"
        self.plugin = load_plugin()
        self.cap = self.plugin.captures
        self.w = self.cap.CaptureWriter(self.vault, self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, h, m):
        return dt.datetime(2026, 10, 18, h, m, tzinfo=TZ)


class BrainDump(Base):
    def test_new_note(self):
        r = self.w.brain_dump("<@123> Pass-through chess board\nwith a camera", "111", "", self.at(9, 5))
        self.assertTrue(r.saved)
        self.assertEqual(r.path, "05-Brain-Dump/2026-10-18-0905-pass-through-chess-board.md")
        text = (self.vault / r.path).read_text()
        self.assertIn('title: "Pass-through chess board"', text)
        self.assertIn('discord_message: "111"', text)
        self.assertIn("created: 2026-10-18", text)
        self.assertTrue(text.endswith("Pass-through chess board\nwith a camera\n"))

    def test_thread_follow_up_appends(self):
        first = self.w.brain_dump("idea one", "111", "", self.at(9, 5))
        r = self.w.brain_dump("also add sound", "222", "111", self.at(9, 20))
        self.assertEqual(r.path, first.path)
        text = (self.vault / first.path).read_text()
        self.assertIn("**Follow-up 09:20:** also add sound", text)
        self.assertEqual(len(list((self.vault / "05-Brain-Dump").iterdir())), 1)

    def test_thread_question_not_saved(self):
        first = self.w.brain_dump("idea one", "111", "", self.at(9, 5))
        r = self.w.brain_dump("would that work?", "222", "111", self.at(9, 20))
        self.assertFalse(r.saved)
        self.assertNotIn("would that work", (self.vault / first.path).read_text())

    def test_never_overwrites(self):
        a = self.w.brain_dump("same", "1", "", self.at(9, 5))
        b = self.w.brain_dump("same", "2", "", self.at(9, 5))
        self.assertNotEqual(a.path, b.path)
        self.assertTrue(b.path.endswith("-2.md"))

    def test_blocked_folder(self):
        with self.assertRaises(ValueError):
            self.w._write_new("01-Personal", "x.md", "x")


class DailyGoals(Base):
    def test_goals_saved(self):
        r = self.w.daily_goals("Today's goals:\n1. **Finish** Phase 6\n- gym\n- [ ] read ch 3", self.at(8, 0))
        self.assertTrue(r.saved)
        text = (self.vault / r.path).read_text()
        self.assertIn("type: daily\n", text)
        self.assertIn("date: 2026-10-18\n", text)
        self.assertTrue(text.endswith("- [ ] Finish Phase 6\n- [ ] gym\n- [ ] read ch 3\n"))

    def test_question_not_saved(self):
        r = self.w.daily_goals("what were my goals yesterday?", self.at(8, 0))
        self.assertFalse(r.saved)
        self.assertEqual(list((self.vault / "_inbox").iterdir()), [])


class FocusSessions(Base):
    def test_start_then_end(self):
        r = self.w.focus_session("start write lab report", self.at(14, 5))
        self.assertTrue(r.saved)
        self.assertEqual(list((self.vault / "_inbox").iterdir()), [])
        r = self.w.focus_session("done: drafted intro and methods", self.at(14, 50))
        self.assertEqual(r.path, "_inbox/2026-10-18-1405-write-lab-report.md")
        text = (self.vault / r.path).read_text()
        for line in ("type: focus-log", "date: 2026-10-18", 'day: "[[2026-10-18]]"',
                     'task: "write lab report"', "duration: 45",
                     "**What got done:** drafted intro and methods"):
            self.assertIn(line, text)
        self.assertFalse((self.state / "focus-session.json").exists())

    def test_end_without_session(self):
        r = self.w.focus_session("stop", self.at(10, 0))
        self.assertFalse(r.saved)
        self.assertIn("none is running", r.summary)

    def test_new_start_closes_previous(self):
        self.w.focus_session("start A", self.at(9, 0))
        r = self.w.focus_session("start B", self.at(9, 30))
        self.assertIn("previous session", r.summary)
        self.assertTrue((self.vault / "_inbox/2026-10-18-0900-a.md").exists())

    def test_other_text_ignored(self):
        self.assertFalse(self.w.focus_session("how long did I focus this week", self.at(9, 0)).saved)


class FakeAdapter:
    def __init__(self):
        self.sent = []
        self._allowed_user_ids = {"42"}

    async def send(self, chat_id, content, **_kw):
        self.sent.append((chat_id, content))


class FakeRaw:
    def __init__(self):
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class Hook(Base):
    def setUp(self):
        super().setUp()
        cfg = json.loads((HERE / "routing.json").read_text())
        cfg["captures"]["vault_path"] = str(self.vault)
        self.cfg_path = Path(self.tmp.name) / "routing.json"
        self.cfg_path.write_text(json.dumps(cfg))
        self.plugin._CONFIG_PATH = self.cfg_path
        self.plugin._config_mtime = 0.0
        os.environ.pop("DISCORD_ALLOWED_USERS", None)  # the adapter's list is what counts

    def dispatch(self, channel, text, user="42", chat_type="group", msg_type="text", thread_id=None):
        adapter = FakeAdapter()
        source = types.SimpleNamespace(
            platform="discord", chat_type=chat_type, chat_id="900", parent_chat_id=None,
            chat_name=f"Server / #{channel}", user_id=user, user_name="nathan", thread_id=thread_id,
            is_bot=False)
        event = types.SimpleNamespace(
            source=source, text=text, message_id="777", channel_prompt=None, raw_message=FakeRaw(),
            message_type=types.SimpleNamespace(value=msg_type), timestamp=dt.datetime(2026, 10, 18, 9, 5))
        gateway = types.SimpleNamespace(
            adapters={"discord": adapter}, _session_model_overrides={},
            _session_key_for_source=lambda s: "k")
        result = asyncio.run(self.plugin._on_pre_gateway_dispatch(event=event, gateway=gateway))
        return event, adapter, result

    def test_brain_dump_saved_and_model_told(self):
        event, _, result = self.dispatch("brain-dump", "robot arm idea")
        self.assertIsNone(result)  # message still goes to the model for the reply
        notes = list((self.vault / "05-Brain-Dump").iterdir())
        self.assertEqual(len(notes), 1)
        self.assertIn("CAPTURE DONE BY THE SYSTEM", event.channel_prompt)
        self.assertIn(notes[0].name, event.channel_prompt)
        self.assertEqual(event.raw_message.reactions, ["📝"])

    def test_other_user_not_saved(self):
        self.dispatch("brain-dump", "spam", user="99")
        self.assertEqual(list((self.vault / "05-Brain-Dump").iterdir()), [])

    def test_voice_note_not_saved(self):
        event, adapter, _ = self.dispatch("brain-dump", "", msg_type="voice")
        self.assertEqual(list((self.vault / "05-Brain-Dump").iterdir()), [])
        self.assertIn("Voice notes", adapter.sent[0][1])

    def test_non_capture_channel_untouched(self):
        event, _, _ = self.dispatch("general-chat", "hello")
        self.assertNotIn("CAPTURE", event.channel_prompt or "")
        self.assertEqual(list((self.vault / "_inbox").iterdir()), [])

    def test_opus_escalation_skips_capture(self):
        self.dispatch("brain-dump", "!opus think hard about this")
        self.assertEqual(list((self.vault / "05-Brain-Dump").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
