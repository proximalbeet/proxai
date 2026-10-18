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
        cfg["security"]["vault_path"] = str(self.vault)  # never the real vault
        self.plugin.guard.log = None
        self.plugin.guard.chat_channels.clear()
        self.cfg_path = Path(self.tmp.name) / "routing.json"
        self.cfg_path.write_text(json.dumps(cfg))
        self.plugin._CONFIG_PATH = self.cfg_path
        self.plugin._config_mtime = 0.0
        os.environ.pop("DISCORD_ALLOWED_USERS", None)  # the adapter's list is what counts

    def dispatch(self, channel, text, user="42", chat_type="group", msg_type="text", thread_id=None,
                 transcript=None, channels=None):
        adapter = FakeAdapter()
        if channels:
            adapter._client = types.SimpleNamespace(
                get_channel=lambda cid: types.SimpleNamespace(name=channels[cid]) if cid in channels else None)
        self.plugin.guard.chat_channels.clear()  # each call is a different channel with the same fake ID
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
        self.stt_calls = 0
        if transcript is not None:
            async def transcribe(ev, _adapter, _source, text, **_kw):
                self.stt_calls += 1
                return (f'[voice] "{transcript}"', [transcript] if transcript else [])
            gateway._transcribe_and_echo_pending_voice = transcribe
        result = asyncio.run(self.plugin._on_pre_gateway_dispatch(event=event, gateway=gateway))
        return event, adapter, result

    def test_voice_brain_dump_saved_from_transcript(self):
        event, adapter, _ = self.dispatch("brain-dump", "", msg_type="voice",
                                          transcript="Robot arm that sorts Lego by colour.")
        notes = list((self.vault / "05-Brain-Dump").iterdir())
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text()
        self.assertIn("input: voice", text)
        self.assertIn("Robot arm that sorts Lego by colour.", text)
        self.assertIn("robot-arm", notes[0].name)
        self.assertEqual(self.stt_calls, 1)
        self.assertIn("CAPTURE DONE BY THE SYSTEM", event.channel_prompt)
        self.assertEqual(adapter.sent, [])

    def test_voice_goals_one_per_sentence(self):
        self.dispatch("daily-goals", "", msg_type="voice",
                      transcript="Finish the slides. Call the dentist! Go for a run.")
        note = next((self.vault / "_inbox").glob("*daily-goals*.md")).read_text()
        self.assertIn("- [ ] Finish the slides\n- [ ] Call the dentist\n- [ ] Go for a run\n", note)
        self.assertIn("input: voice", note)

    def test_voice_focus_start(self):
        event, _, _ = self.dispatch("focus-sessions", "", msg_type="voice", transcript="Start wiring the arm.")
        self.assertIn("started a focus session at 09:05 on: wiring the arm", event.channel_prompt)

    def test_voice_not_transcribed_not_saved(self):
        event, adapter, _ = self.dispatch("brain-dump", "", msg_type="voice", transcript="")
        self.assertEqual(list((self.vault / "05-Brain-Dump").iterdir()), [])
        self.assertIn("couldn't transcribe", adapter.sent[0][1])

    def test_channel_mention_resolved_in_title(self):
        self.dispatch("brain-dump", "save a note in <#1500997711506182244> about gears",
                      channels={1500997711506182244: "brain-dump"})
        note = next((self.vault / "05-Brain-Dump").iterdir())
        self.assertIn("save-a-note-in-brain-dump", note.name)
        self.assertIn('title: "save a note in #brain-dump about gears"', note.read_text())

    def test_unknown_channel_mention(self):
        self.dispatch("brain-dump", "see <#123> later")
        note = next((self.vault / "05-Brain-Dump").iterdir())
        self.assertIn('title: "see #channel later"', note.read_text())

    def test_brain_dump_saved_and_model_told(self):
        event, _, result = self.dispatch("brain-dump", "robot arm idea")
        self.assertIsNone(result)  # message still goes to the model for the reply
        notes = list((self.vault / "05-Brain-Dump").iterdir())
        self.assertEqual(len(notes), 1)
        self.assertIn("CAPTURE DONE BY THE SYSTEM", event.channel_prompt)
        self.assertIn(notes[0].name, event.channel_prompt)
        self.assertEqual(event.raw_message.reactions, ["📝"])

    def test_history_dropped_only_where_off(self):
        for chat_id, channel, kept in (("910", "brain-dump", False), ("911", "general-chat", True)):
            adapter = FakeAdapter()
            source = types.SimpleNamespace(
                platform="discord", chat_type="group", chat_id=chat_id, parent_chat_id=None,
                chat_name=f"Server / #{channel}", user_id="42", user_name="nathan", thread_id=None,
                is_bot=False)
            event = types.SimpleNamespace(
                source=source, text="hello?", message_id="779", channel_prompt=None, raw_message=FakeRaw(),
                message_type=types.SimpleNamespace(value="text"), timestamp=dt.datetime(2026, 10, 18, 9, 5),
                channel_context="[Recent channel messages]\nold stuff")
            gateway = types.SimpleNamespace(adapters={"discord": adapter}, _session_model_overrides={},
                                            _session_key_for_source=lambda s: "k")
            asyncio.run(self.plugin._on_pre_gateway_dispatch(event=event, gateway=gateway))
            self.assertEqual(event.channel_context is not None, kept, channel)

    def test_channel_from_chat_name(self):
        f = self.plugin.channel_from_chat_name
        self.assertEqual(f("Server / #brain-dump"), "brain-dump")
        self.assertEqual(f("Server / #brain-dump / robot #2 ideas"), "brain-dump")
        self.assertEqual(f("Server / #vr-ideas / a / b"), "vr-ideas")
        self.assertEqual(f("general-chat"), "general-chat")
        self.assertEqual(f("Server / Forum / post"), "")

    def test_thread_cache_miss_routes_by_parent(self):
        # No Discord client, so the cache "misses"; the thread title contains '#'.
        self.plugin.guard.chat_channels.clear()
        event, _, _ = self.dispatch_thread("Server / #general-chat / fix #3 build", "hi")
        self.assertIn("general-chat", event.channel_prompt)
        overrides = self.last_gateway._session_model_overrides["k"]
        self.assertEqual(overrides["model"], "claude-sonnet-5-5")

    def dispatch_thread(self, chat_name, text):
        adapter = FakeAdapter()
        source = types.SimpleNamespace(
            platform="discord", chat_type="thread", chat_id="901", parent_chat_id="900",
            chat_name=chat_name, user_id="42", user_name="nathan", thread_id="901", is_bot=False)
        event = types.SimpleNamespace(
            source=source, text=text, message_id="778", channel_prompt=None, raw_message=FakeRaw(),
            message_type=types.SimpleNamespace(value="text"), timestamp=dt.datetime(2026, 10, 18, 9, 5))
        self.last_gateway = types.SimpleNamespace(
            adapters={"discord": adapter}, _session_model_overrides={},
            _session_key_for_source=lambda s: "k")
        result = asyncio.run(self.plugin._on_pre_gateway_dispatch(event=event, gateway=self.last_gateway))
        return event, adapter, result

    def test_route_logged_to_security_log(self):
        self.dispatch("general-chat", "hello")
        log = next((self.vault / "07-Security/ProxAI-Audit").glob("*.md")).read_text()
        self.assertIn("**ROUTE** · #general-chat · nathan (42) → Claude Sonnet 5.5", log)

    def test_other_user_not_saved(self):
        self.dispatch("brain-dump", "spam", user="99")
        self.assertEqual(list((self.vault / "05-Brain-Dump").iterdir()), [])

    def test_voice_note_without_gateway_stt_not_saved(self):
        event, adapter, _ = self.dispatch("brain-dump", "", msg_type="voice")
        self.assertEqual(list((self.vault / "05-Brain-Dump").iterdir()), [])
        self.assertIn("couldn't transcribe", adapter.sent[0][1])

    def test_non_capture_channel_untouched(self):
        event, _, _ = self.dispatch("general-chat", "hello")
        self.assertNotIn("CAPTURE", event.channel_prompt or "")
        self.assertEqual(list((self.vault / "_inbox").iterdir()), [])

    def test_opus_escalation_skips_capture(self):
        self.dispatch("brain-dump", "!opus think hard about this")
        self.assertEqual(list((self.vault / "05-Brain-Dump").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
