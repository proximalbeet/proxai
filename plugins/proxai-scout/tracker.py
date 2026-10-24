"""The scout's gig board (decided with Nathan 2026-10-24): every gig and app idea the scout picks is kept
with a status, his notes and an hours estimate, so an app can manage them.

Gigs:  new -> interested -> pitched -> won -> done  (or passed)
Ideas: new -> interested -> building -> built        (or passed)

* ~/.local/state/proxai/scout/tracker.json: {"items": {id: item}}; id = 12 hex chars from the post.
* The phone (a ProxAI-built app, through ProxMobile and the apps server) can't write it: the apps
  server only drops update requests into scout/inbox/. The scout plugin applies them here (the only
  writer), including "block N hours on my calendar" (ProxAI calendar, through proxai-calendar).
* Chat changes it too: "more like gig 1" = interested, "not interested in gig 2" = passed,
  "I pitched gig 3" = pitched, "I got gig 3" = won.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

GIG_STATUSES = ("new", "interested", "pitched", "won", "done", "passed")
IDEA_STATUSES = ("new", "interested", "building", "built", "passed")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_NOTES = 2000
MAX_ITEMS = 1000
KEEP_CLOSED_DAYS = 90
MAX_BLOCK_HOURS = 12
INBOX_MAX = 50          # requests applied per pass; more wait for the next one


def _dir() -> Path:
    return Path(os.getenv("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser() / "scout"


def tracker_file() -> Path:
    return _dir() / "tracker.json"


def inbox() -> Path:
    return _dir() / "inbox"


def item_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@contextmanager
def _locked():
    _dir().mkdir(parents=True, exist_ok=True)
    with (_dir() / ".tracker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _load() -> dict:
    try:
        data = json.loads(tracker_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("items", {})
    return data


def _save(data: dict) -> None:
    tmp = tracker_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, tracker_file())


def load() -> dict:
    return _load()


def add_picks(gigs: list[dict], ideas: list[dict], run: str) -> int:
    """New picks go on the board as "new"; picks already there keep their status. Returns how many were new."""
    fields_gig = ("title", "url", "source", "fit", "pay", "effort", "why", "pitch")
    fields_idea = ("title", "url", "source", "score", "name", "problem", "app", "effort", "money")
    added = 0
    with _locked():
        data = _load()
        for lane, picks, fields in (("gigs", gigs, fields_gig), ("ideas", ideas, fields_idea)):
            for p in picks:
                iid = item_id(p["key"])
                if iid in data["items"]:
                    continue
                data["items"][iid] = {"id": iid, "lane": lane, **{f: p.get(f) for f in fields},
                                      "status": "new", "notes": "", "hours": None, "calendar": None,
                                      "found": _now(), "updated": _now(), "run": run}
                added += 1
        _prune(data)
        _save(data)
    return added


def _prune(data: dict) -> None:
    cutoff = (dt.datetime.now() - dt.timedelta(days=KEEP_CLOSED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    items = data["items"]
    for iid in [i for i, it in items.items() if it["status"] in ("passed", "done", "built") and it["updated"] < cutoff]:
        items.pop(iid)
    if len(items) > MAX_ITEMS:
        for iid in sorted(items, key=lambda i: items[i]["updated"])[:len(items) - MAX_ITEMS]:
            items.pop(iid)


def statuses(lane: str) -> tuple[str, ...]:
    return GIG_STATUSES if lane == "gigs" else IDEA_STATUSES


def set_status(key_or_id: str, status: str, note: str | None = None) -> dict | None:
    """Chat path. Returns the item, or None when it isn't on the board (or the status doesn't fit its lane)."""
    iid = key_or_id if ID_RE.match(key_or_id) else item_id(key_or_id)
    with _locked():
        data = _load()
        it = data["items"].get(iid)
        if not it or status not in statuses(it["lane"]):
            return None
        it["status"] = status
        if note:
            it["notes"] = (it["notes"] + ("\n" if it["notes"] else "") + note)[-MAX_NOTES:]
        it["updated"] = _now()
        _save(data)
        return dict(it)


# ---- requests from the phone ----------------------------------------------------------------------

class Bad(Exception):
    pass


def check_request(req) -> dict:
    """The apps server and this module both run it: only these fields, these sizes."""
    if not isinstance(req, dict):
        raise Bad("not an object")
    extra = set(req) - {"id", "status", "notes", "hours", "block"}
    if extra:
        raise Bad(f"unknown fields: {sorted(extra)}")
    if not isinstance(req.get("id"), str) or not ID_RE.match(req["id"]):
        raise Bad("id must be 12 hex characters")
    out = {"id": req["id"]}
    if "status" in req:
        if req["status"] not in set(GIG_STATUSES) | set(IDEA_STATUSES):
            raise Bad("unknown status")
        out["status"] = req["status"]
    if "notes" in req:
        if not isinstance(req["notes"], str) or len(req["notes"]) > MAX_NOTES:
            raise Bad(f"notes must be text, at most {MAX_NOTES} characters")
        out["notes"] = req["notes"]
    if "hours" in req:
        h = req["hours"]
        if h is not None and (isinstance(h, bool) or not isinstance(h, (int, float)) or not 0 <= h <= 500):
            raise Bad("hours must be a number from 0 to 500, or null")
        out["hours"] = h
    if "block" in req:
        b = req["block"]
        if not isinstance(b, dict) or set(b) - {"start", "hours"}:
            raise Bad("block must be {start, hours}")
        try:
            start = dt.datetime.fromisoformat(str(b.get("start")))
        except ValueError:
            raise Bad("block.start must be a date and time like 2026-10-25T14:00") from None
        hours = b.get("hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0.25 <= hours <= MAX_BLOCK_HOURS:
            raise Bad(f"block.hours must be from 0.25 to {MAX_BLOCK_HOURS}")
        out["block"] = {"start": start.replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec="minutes"),
                        "hours": float(hours)}
    if len(out) == 1:
        raise Bad("nothing to change")
    return out


def apply_inbox(calendar_create, notify) -> list[dict]:
    """Apply the phone's queued requests, oldest first. calendar_create(title, start, end, description) -> event
    dict (raises on failure); notify(title, text). Returns what happened, one entry per request."""
    folder = inbox()
    try:
        files = sorted(p for p in folder.iterdir() if p.suffix == ".json")[:INBOX_MAX]
    except OSError:
        return []
    done = []
    for f in files:
        try:
            req = check_request(json.loads(f.read_text(encoding="utf-8")))
            done.append(_apply(req, calendar_create, notify))
        except (Bad, ValueError, OSError) as exc:
            done.append({"file": f.name, "ok": False, "error": str(exc)[:200]})
        finally:
            f.unlink(missing_ok=True)
    return done


def _apply(req: dict, calendar_create, notify) -> dict:
    with _locked():
        data = _load()
        it = data["items"].get(req["id"])
        if not it:
            return {"id": req["id"], "ok": False, "error": "not on the board"}
        if "status" in req:
            if req["status"] not in statuses(it["lane"]):
                return {"id": it["id"], "ok": False, "error": f"'{req['status']}' isn't a status for {it['lane']}"}
            it["status"] = req["status"]
        if "notes" in req:
            it["notes"] = req["notes"]
        if "hours" in req:
            it["hours"] = req["hours"]
        it["updated"] = _now()
        _save(data)
        item = dict(it)
    result = {"id": item["id"], "ok": True}
    if "block" in req:
        start = dt.datetime.fromisoformat(req["block"]["start"])
        end = start + dt.timedelta(hours=req["block"]["hours"])
        label = item.get("title") or item.get("name") or "scout item"
        try:
            ev = calendar_create(f"Gig: {label[:70]}", start.isoformat(timespec="minutes"),
                                 end.isoformat(timespec="minutes"),
                                 f"{item.get('url') or ''}\n{item.get('notes') or ''}\n(From ProxAI's scout gig board.)".strip())
        except Exception as exc:     # the board change above still stands
            notify("Calendar block failed", f"{label[:80]}: {str(exc)[:200]}")
            return {**result, "calendar_error": str(exc)[:200]}
        with _locked():
            data = _load()
            if item["id"] in data["items"]:
                data["items"][item["id"]]["calendar"] = {"event_id": ev.get("id") or ev.get("event_id"),
                                                         "start": req["block"]["start"], "hours": req["block"]["hours"]}
                _save(data)
        notify(f"📅 Blocked {req['block']['hours']:g} h on your ProxAI calendar",
               f"{label[:80]}\n{start.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')}")
        result["calendar"] = True
    return result
