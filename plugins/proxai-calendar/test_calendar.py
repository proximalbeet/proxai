"""Tests for the ProxAI calendar plugin against a fake Google API (no network, no account).

Run: python3 plugins/proxai-calendar/test_calendar.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="cal-test-", dir=os.environ.get("TMPDIR")))
os.environ["PROXAI_CALENDAR_DIR"] = str(TMP / "cfg")
os.environ["PROXAI_CALENDAR_LOG"] = str(TMP / "calendar.log")

spec = importlib.util.spec_from_file_location("proxai_calendar", HERE / "__init__.py",
                                              submodule_search_locations=[str(HERE)])
plugin = importlib.util.module_from_spec(spec)
sys.modules["proxai_calendar"] = plugin
spec.loader.exec_module(plugin)
core = sys.modules["proxai_calendar.core"]

APP, FB = core.SCOPE_APP, core.SCOPE_FREEBUSY
PROXAI = "abc123@group.calendar.google.com"


class FakeGoogle:
    def __init__(self, scope=f"{APP} {FB}"):
        self.scope, self.events, self.requests, self.n = scope, {}, [], 0

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        data = json.loads(body) if body and headers.get("Content-Type") == "application/json" else None
        self.requests.append((method, u.path, q, data))
        if url == core.TOKEN_URL:
            return 200, {"access_token": "at", "expires_in": 3600, "scope": self.scope}
        assert headers["Authorization"] == "Bearer at"
        path = urllib.parse.unquote(u.path.removeprefix("/calendar/v3"))
        if path == "/freeBusy":
            return 200, {"calendars": {"primary": {"busy": [{"start": "2026-10-20T13:00:00Z",
                                                             "end": "2026-10-20T14:00:00Z"}]}}}
        parts = path.strip("/").split("/")  # calendars, <id>, events, [<eid>]
        if parts[1] != PROXAI:  # calendar.app.created can't touch other calendars
            return 403, {"error": {"message": "Insufficient Permission"}}
        if method == "POST":
            self.n += 1
            ev = {**data, "id": f"ev{self.n}", "htmlLink": "https://calendar.google.com/x"}
            self.events[ev["id"]] = ev
            return 200, ev
        if len(parts) == 3:
            return 200, {"items": list(self.events.values())}
        eid = parts[3]
        if eid not in self.events:
            return 404, {"error": {"message": "Not Found"}}
        if method == "GET":
            return 200, self.events[eid]
        if method == "PATCH":
            self.events[eid].update(data)
            return 200, self.events[eid]
        if method == "DELETE":
            del self.events[eid]
            return 204, {}
        return 400, {}


def setup_files(scope=f"{APP} {FB}", mode=0o600):
    cfg = TMP / "cfg"
    core.write_private_json(cfg / "client_secret.json", {"installed": {"client_id": "id", "client_secret": "s"}})
    core.write_private_json(cfg / "token.json", {"refresh_token": "rt", "scope": scope})
    core.write_private_json(cfg / "calendar.json", {"calendar_id": PROXAI, "time_zone": "America/Detroit"})
    os.chmod(cfg / "token.json", mode)


class CalendarTest(unittest.TestCase):
    def setUp(self):
        setup_files()
        self.g = FakeGoogle()
        plugin._cal = core.Calendar(TMP / "cfg", http=self.g)
        plugin._writes.clear()
        plugin._pending.clear()

    def call(self, **args):
        return json.loads(plugin._handle_calendar(args))

    def test_create_list_move(self):
        r = self.call(action="create", title="Study MIPS", start="2026-10-20T14:00", end="2026-10-20T15:30")
        self.assertTrue(r["ok"], r)
        method, path, q, body = self.g.requests[-1]
        self.assertIn(PROXAI, urllib.parse.unquote(path))
        self.assertEqual(q["sendUpdates"], "none")
        self.assertNotIn("attendees", body)
        self.assertEqual(body["start"], {"dateTime": "2026-10-20T14:00:00", "timeZone": "America/Detroit"})
        eid = r["created"]["event_id"]
        self.assertEqual([e["event_id"] for e in self.call(action="list")["events"]], [eid])
        r = self.call(action="update", event_id=eid, start="2026-10-21T09:00")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["updated"]["start"], "2026-10-21T09:00:00")
        self.assertEqual(r["updated"]["end"], "2026-10-21T10:30:00")  # length kept
        self.assertIn('"action": "update"', (TMP / "calendar.log").read_text())

    def test_all_day_default_end(self):
        r = self.call(action="create", title="Exam", start="2026-10-22")
        self.assertEqual((r["created"]["start"], r["created"]["end"]), ("2026-10-22", "2026-10-23"))

    def test_model_cannot_pick_another_calendar(self):
        # the schema has no calendar field; an extra arg is ignored and the fixed calendar is used
        self.call(action="create", title="x", start="2026-10-20T10:00", calendar_id="primary")
        self.assertIn(PROXAI, urllib.parse.unquote(self.g.requests[-1][1]))

    def test_main_calendar_write_is_refused(self):
        with self.assertRaises(core.CalendarError) as cm:
            plugin._cal.api("POST", "/calendars/primary/events", body={"summary": "no"})
        self.assertIn("permission denied", str(cm.exception))

    def test_delete_needs_nathans_code(self):
        eid = self.call(action="create", title="Gym", start="2026-10-20T18:00")["created"]["event_id"]
        r = self.call(action="delete", event_id=eid)
        self.assertFalse(r["deleted"])
        self.assertIn(eid, self.g.events)  # still there
        code = r["tell_nathan"].split("/calendar-confirm ")[1].split()[0]
        self.assertIn("No pending", plugin._cmd_confirm("wrong"))
        self.assertIn(eid, self.g.events)
        self.assertIn("Deleted", plugin._cmd_confirm(code))
        self.assertNotIn(eid, self.g.events)
        self.assertIn("No pending", plugin._cmd_confirm(code))  # one use only

    def test_broader_token_refused(self):
        setup_files(scope=f"{APP} https://www.googleapis.com/auth/calendar")
        plugin._cal = core.Calendar(TMP / "cfg", http=FakeGoogle())
        r = self.call(action="list")
        self.assertFalse(r["ok"])
        self.assertIn("more access than ProxAI is allowed", r["error"])

    def test_token_file_must_be_private(self):
        setup_files(mode=0o644)
        plugin._cal = core.Calendar(TMP / "cfg", http=self.g)
        r = self.call(action="list")
        self.assertIn("chmod 600", r["error"])

    def test_busy_shows_times_only(self):
        r = self.call(action="busy", start="2026-10-20T00:00", end="2026-10-21T00:00")
        self.assertEqual(r["busy"], [{"start": "2026-10-20T13:00:00Z", "end": "2026-10-20T14:00:00Z"}])
        setup_files(scope=APP)
        plugin._cal = core.Calendar(TMP / "cfg", http=FakeGoogle(scope=APP))
        self.assertIn("weren't allowed", self.call(action="busy")["error"])

    def test_rate_limit(self):
        for i in range(plugin._WRITES_PER_HOUR):
            self.assertTrue(self.call(action="create", title=f"e{i}", start="2026-10-20T10:00")["ok"])
        r = self.call(action="create", title="one too many", start="2026-10-20T10:00")
        self.assertIn("in an hour", r["error"])

    def test_bad_input(self):
        for args, msg in (({"title": "", "start": "2026-10-20T10:00"}, "title is empty"),
                          ({"title": "x", "start": "tomorrow"}, "bad date/time"),
                          ({"title": "x", "start": "2026-10-20T10:00", "end": "2026-10-20T09:00"}, "after start"),
                          ({"title": "x", "start": "2026-10-20", "end": "2026-11-20"}, "longer than"),
                          ({"title": "x" * 300, "start": "2026-10-20T10:00"}, "longer than 200")):
            r = self.call(action="create", **args)
            self.assertFalse(r["ok"])
            self.assertIn(msg, r["error"])
        self.assertIn("bad event_id", self.call(action="update", event_id="../primary", title="x")["error"])
        self.assertIn("range longer", self.call(action="list", start="2026-01-01", end="2026-12-31")["error"])

    def test_register(self):
        seen = []

        class Ctx:
            def register_tool(self, **k): seen.append(("tool", k["name"]))
            def register_command(self, name, fn, **k): seen.append(("cmd", name))
            def register_hook(self, name, fn): seen.append(("hook", name))
        plugin.register(Ctx())
        self.assertEqual(seen, [("tool", "calendar"), ("cmd", "calendar"), ("cmd", "calendar-confirm"),
                                ("hook", "pre_gateway_dispatch")])


if __name__ == "__main__":
    unittest.main()
