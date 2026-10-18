"""Google Calendar access for ProxAI, limited to its own "ProxAI" calendar (checklist Phase 9).

Python stdlib only (urllib). Shared by the Hermes plugin (__init__.py) and setup_calendar.py.

Permission model
* OAuth scopes: ``calendar.app.created`` (calendars this app created + their events: the only
  place it can write), optionally ``calendar.events.readonly`` (read Nathan's own events, never
  change them; Nathan 10-18) and ``calendar.freebusy`` (busy/free times only). A token with any
  other scope is refused, so a broader token can't be swapped in.
* The calendar id for writes comes from ``calendar.json`` written at setup, never from the model.
  Reads of Nathan's events go to his ``primary`` calendar plus any he added with
  ``setup_calendar.py --add-read-calendar`` (``read_calendars`` in calendar.json).
* Events never get attendees, and every write uses ``sendUpdates=none``: no invites or emails.

Files (``~/.hermes/profiles/proxai/google-calendar/``, folder 700, files 600, not in git):
  client_secret.json  OAuth desktop client from Google Cloud Console
  token.json          refresh token + granted scopes
  calendar.json       {"calendar_id": ..., "time_zone": ...}
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_DIR = Path(os.environ.get("PROXAI_CALENDAR_DIR",
                                 Path.home() / ".hermes/profiles/proxai/google-calendar"))
LOG_PATH = Path(os.environ.get("PROXAI_CALENDAR_LOG", Path.home() / ".local/state/proxai/calendar.log"))

SCOPE_APP = "https://www.googleapis.com/auth/calendar.app.created"
SCOPE_FREEBUSY = "https://www.googleapis.com/auth/calendar.freebusy"
SCOPE_READ_EVENTS = "https://www.googleapis.com/auth/calendar.events.readonly"
ALLOWED_SCOPES = {SCOPE_APP, SCOPE_FREEBUSY, SCOPE_READ_EVENTS}
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

MAX_TITLE, MAX_DESC = 200, 2000
MAX_EVENT_DAYS, MAX_RANGE_DAYS = 7, 92
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
CAL_ID_RE = re.compile(r"^(primary|[A-Za-z0-9._%+#-]{1,200}@[A-Za-z0-9.-]{1,200})$")


class CalendarError(Exception):
    """A problem to report back plainly (never a crash)."""


# --------------------------------------------------------------------------- http

def urllib_http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"raw": raw[:300].decode(errors="replace")}


# --------------------------------------------------------------------------- files

def _private_json(path: Path) -> dict:
    if not path.exists():
        raise CalendarError(f"not set up yet: {path.name} missing (run setup_calendar.py)")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CalendarError(f"{path.name} is readable by other users; run: chmod 600 {path}")
    return json.loads(path.read_text())


def write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def check_scopes(granted: str | list) -> set[str]:
    scopes = set(granted.split() if isinstance(granted, str) else granted)
    extra = scopes - ALLOWED_SCOPES
    if extra:
        raise CalendarError("token has more access than ProxAI is allowed "
                            f"({', '.join(sorted(extra))}); delete token.json and run setup again")
    if SCOPE_APP not in scopes:
        raise CalendarError("token is missing calendar.app.created; run setup again")
    return scopes


# --------------------------------------------------------------------------- times

def parse_when(value: str, tz: str) -> dict:
    """'2026-10-20' → all-day; '2026-10-20T14:00' (local) or with offset → timed."""
    v = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        date.fromisoformat(v)
        return {"date": v}
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise CalendarError(f"bad date/time '{v}': use YYYY-MM-DD or YYYY-MM-DDTHH:MM") from None
    if dt.tzinfo is None:
        return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz}
    return {"dateTime": dt.isoformat(), "timeZone": tz}


def _as_dt(when: dict, tz: str = "UTC") -> datetime:
    """Aware datetime for comparisons; dates and local times are read in the calendar's zone."""
    zone = ZoneInfo(when.get("timeZone") or tz)
    if "date" in when:
        return datetime.fromisoformat(when["date"]).replace(tzinfo=zone)
    dt = datetime.fromisoformat(when["dateTime"].replace("Z", "+00:00"))
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt


def build_times(start: str, end: str | None, tz: str) -> tuple[dict, dict]:
    s = parse_when(start, tz)
    if end:
        e = parse_when(end, tz)
    elif "date" in s:
        e = {"date": (date.fromisoformat(s["date"]) + timedelta(days=1)).isoformat()}
    else:
        e = {"dateTime": (datetime.fromisoformat(s["dateTime"]) + timedelta(hours=1)).isoformat(),
             "timeZone": tz}
    if ("date" in s) != ("date" in e):
        raise CalendarError("start and end must both be dates or both be date-times")
    span = _as_dt(e, tz) - _as_dt(s, tz)
    if span <= timedelta(0):
        raise CalendarError("end must be after start")
    if span > timedelta(days=MAX_EVENT_DAYS):
        raise CalendarError(f"events longer than {MAX_EVENT_DAYS} days aren't allowed")
    return s, e


def range_bounds(start: str | None, end: str | None) -> tuple[str, str]:
    def read(v: str) -> datetime:
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone()
        except ValueError:
            raise CalendarError(f"bad date/time '{v}': use YYYY-MM-DD or YYYY-MM-DDTHH:MM") from None
    s = read(start) if start else datetime.now().astimezone()
    e = read(end) if end else s + timedelta(days=7)
    if e <= s:
        raise CalendarError("range end must be after start")
    if e - s > timedelta(days=MAX_RANGE_DAYS):
        raise CalendarError(f"range longer than {MAX_RANGE_DAYS} days")
    return s.isoformat(), e.isoformat()


# --------------------------------------------------------------------------- client

class Calendar:
    def __init__(self, config_dir: Path = CONFIG_DIR, http=urllib_http):
        self.dir, self.http = Path(config_dir), http
        self._access, self._expires = None, 0.0

    # -- auth
    def _token(self) -> str:
        # A new sign-in (setup run again) replaces token.json: pick it up without a restart.
        try:
            mtime = (self.dir / "token.json").stat().st_mtime
        except OSError:
            mtime = None
        if self._access and time.time() < self._expires - 60 and mtime == getattr(self, "_token_mtime", None):
            return self._access
        self._token_mtime = mtime
        tok = _private_json(self.dir / "token.json")
        self.scopes = check_scopes(tok.get("scope", ""))
        secret = _private_json(self.dir / "client_secret.json")
        client = secret.get("installed") or secret.get("web") or secret
        body = urllib.parse.urlencode({
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
        }).encode()
        status, data = self.http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200 or "access_token" not in data:
            raise CalendarError(f"Google sign-in failed ({status}: {data.get('error', '')}); "
                                "the refresh token may have expired, run setup again")
        if data.get("scope"):
            self.scopes = check_scopes(data["scope"])
        self._access, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._access

    def config(self) -> dict:
        cfg = _private_json(self.dir / "calendar.json")
        if not cfg.get("calendar_id"):
            raise CalendarError("calendar.json has no calendar_id; run setup again")
        return cfg

    def api(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        status, resp = self.http(method, url, headers, data)
        if status in (200, 201, 204):
            return resp
        msg = (resp.get("error") or {}).get("message") if isinstance(resp.get("error"), dict) else resp.get("error")
        if status in (401, 403):
            raise CalendarError(f"permission denied by Google ({status}): {msg or 'not allowed'}")
        if status == 404:
            raise CalendarError("not found on the ProxAI calendar (ProxAI can only see its own events)")
        raise CalendarError(f"Google Calendar returned {status}: {msg or resp}")

    def _cal_path(self) -> str:
        return "/calendars/" + urllib.parse.quote(self.config()["calendar_id"], safe="")

    @staticmethod
    def _event_path(event_id: str) -> str:
        if not EVENT_ID_RE.match(str(event_id or "")):
            raise CalendarError("bad event_id")
        return "/events/" + urllib.parse.quote(event_id, safe="")

    # -- reads
    def list_events(self, start: str | None = None, end: str | None = None) -> list[dict]:
        t0, t1 = range_bounds(start, end)
        resp = self.api("GET", self._cal_path() + "/events",
                        {"timeMin": t0, "timeMax": t1, "singleEvents": "true",
                         "orderBy": "startTime", "maxResults": "50"})
        return [slim(e) for e in resp.get("items", [])]

    def get_event(self, event_id: str) -> dict:
        return slim(self.api("GET", self._cal_path() + self._event_path(event_id)))

    def busy(self, start: str | None = None, end: str | None = None) -> list[dict]:
        self._token()
        if SCOPE_FREEBUSY not in getattr(self, "scopes", set()):
            raise CalendarError("busy times weren't allowed at setup (calendar.freebusy not granted)")
        t0, t1 = range_bounds(start, end)
        resp = self.api("POST", "/freeBusy", body={"timeMin": t0, "timeMax": t1, "items": [{"id": "primary"}]})
        cal = (resp.get("calendars") or {}).get("primary", {})
        if cal.get("errors"):
            raise CalendarError(f"busy lookup refused: {cal['errors'][0].get('reason')}")
        return cal.get("busy", [])

    def read_calendars(self) -> list[dict]:
        """Calendars Hermes may READ, set by Nathan with setup_calendar.py (never by the model).
        Default: his primary calendar."""
        cfg = self.config()
        cals = cfg.get("read_calendars") or [{"id": "primary", "label": "Nathan's"}]
        return [c for c in cals if isinstance(c, dict) and CAL_ID_RE.match(str(c.get("id", "")))
                and c["id"] != cfg["calendar_id"]]  # ProxAI's own events come from list_events

    def my_events(self, start: str | None = None, end: str | None = None) -> list[dict]:
        """Nathan's events on the calendars he allowed, read-only. Only title, time and place:
        descriptions and attendee lists are left out (invites are text anyone can send).
        A calendar that can't be read is reported in ``self.warnings`` instead of failing all."""
        self._token()
        if SCOPE_READ_EVENTS not in getattr(self, "scopes", set()):
            raise CalendarError("reading Nathan's own events wasn't allowed at setup "
                                "(calendar.events.readonly not granted); only busy times are visible")
        t0, t1 = range_bounds(start, end)
        out, self.warnings = [], []
        for cal in self.read_calendars():
            try:
                resp = self.api("GET", "/calendars/" + urllib.parse.quote(cal["id"], safe="") + "/events",
                                {"timeMin": t0, "timeMax": t1, "singleEvents": "true",
                                 "orderBy": "startTime", "maxResults": "100"})
            except CalendarError as exc:
                self.warnings.append(f"couldn't read {cal.get('label') or cal['id']}: {exc}")
                continue
            for e in resp.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                s, en = e.get("start", {}), e.get("end", {})
                out.append({"calendar": cal.get("label") or cal["id"],
                            "title": (e.get("summary") or "(busy)")[:200],
                            "start": s.get("dateTime") or s.get("date"),
                            "end": en.get("dateTime") or en.get("date"),
                            "location": (e.get("location") or "")[:200]})
        return out

    def agenda(self, start: str | None = None, end: str | None = None) -> list[dict]:
        """Nathan's events + ProxAI's, sorted by start. Only ProxAI ones carry an event_id."""
        mine = self.my_events(start, end)
        ours = [{"calendar": "ProxAI", "event_id": e["event_id"], "title": e["title"],
                 "start": e["start"], "end": e["end"]} for e in self.list_events(start, end)]
        tz = self.config().get("time_zone", "UTC")

        def key(ev):
            v = ev.get("start") or ""
            try:
                return _as_dt({"date": v} if len(v) == 10 else {"dateTime": v}, tz)
            except (ValueError, TypeError):
                return datetime.max.replace(tzinfo=ZoneInfo("UTC"))
        return sorted(mine + ours, key=key)

    # -- writes (ProxAI calendar only, never invites)
    def create(self, title: str, start: str, end: str | None = None, description: str = "") -> dict:
        title = clean_text(title, MAX_TITLE, "title")
        s, e = build_times(start, end, self.config().get("time_zone", "UTC"))
        body = {"summary": title, "start": s, "end": e,
                "description": clean_text(description, MAX_DESC, "description", required=False),
                "extendedProperties": {"private": {"createdBy": "proxai"}}}
        return slim(self.api("POST", self._cal_path() + "/events", {"sendUpdates": "none"}, body))

    def update(self, event_id: str, title: str | None = None, start: str | None = None,
               end: str | None = None, description: str | None = None) -> dict:
        body: dict = {}
        if title is not None:
            body["summary"] = clean_text(title, MAX_TITLE, "title")
        if description is not None:
            body["description"] = clean_text(description, MAX_DESC, "description", required=False)
        if start is not None or end is not None:
            current = self.api("GET", self._cal_path() + self._event_path(event_id))
            tz = self.config().get("time_zone", "UTC")
            cur_s, cur_e = current.get("start", {}), current.get("end", {})
            new_start = start or cur_s.get("dateTime") or cur_s.get("date")
            if end is None and start is not None:
                # keep the length when only the start moves
                length = _as_dt(cur_e, tz) - _as_dt(cur_s, tz)
                st = parse_when(start, tz)
                if "date" in st:
                    end = (date.fromisoformat(st["date"]) + length).isoformat()
                else:
                    end = (datetime.fromisoformat(st["dateTime"]) + length).isoformat()
            body["start"], body["end"] = build_times(new_start, end or cur_e.get("dateTime") or cur_e.get("date"), tz)
        if not body:
            raise CalendarError("nothing to change")
        return slim(self.api("PATCH", self._cal_path() + self._event_path(event_id),
                             {"sendUpdates": "none"}, body))

    def delete(self, event_id: str) -> None:
        self.api("DELETE", self._cal_path() + self._event_path(event_id), {"sendUpdates": "none"})


def clean_text(value, limit: int, name: str, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise CalendarError(f"{name} is empty")
    if len(text) > limit:
        raise CalendarError(f"{name} is longer than {limit} characters")
    return text


def slim(event: dict) -> dict:
    s, e = event.get("start", {}), event.get("end", {})
    return {"event_id": event.get("id"), "title": event.get("summary", ""),
            "start": s.get("dateTime") or s.get("date"), "end": e.get("dateTime") or e.get("date"),
            "description": (event.get("description") or "")[:300], "link": event.get("htmlLink", "")}


def log(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
