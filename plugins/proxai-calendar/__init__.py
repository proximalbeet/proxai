"""
ProxAI calendar (checklist Phase 9): Hermes manages events on its own "ProxAI" Google calendar
and nothing else. Runs host-side in the gateway (the Docker sandbox stays offline).

* ``calendar`` tool: list / busy / create / update / delete. The calendar is fixed by
  ``calendar.json`` (setup), never chosen by the model. No attendees, no invites.
* Deletes are never done by the model: ``delete`` returns a short code and Nathan types
  ``/calendar-confirm <code>`` (no model in the loop). Codes expire after 30 minutes.
* At most 20 writes per hour (stops a runaway loop).
* Every create / update / delete goes to ``~/.local/state/proxai/calendar.log`` and one line
  in #bot-audit-log.
* Which channels may call the tool is set in discord-routing's ``tool_policy``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque

from .core import Calendar, CalendarError, log

logger = logging.getLogger(__name__)

_AUDIT_CHANNEL = "bot-audit-log"
_WRITES_PER_HOUR = 20
_CONFIRM_SECS = 30 * 60

_gateway = None
_cal = Calendar()
_writes: deque[float] = deque()
_pending: dict[str, dict] = {}  # code -> {"event_id", "title", "start", "expires", "channel", "user"}

SCHEMA = {
    "name": "calendar",
    "description": (
        "Nathan's ProxAI Google calendar: a separate calendar that shows next to his own. You can "
        "only see and change events on this ProxAI calendar, never his other calendars. "
        "list: events on the ProxAI calendar (default next 7 days). "
        "busy: when Nathan is busy on his main calendar (times only, no details), to avoid clashes. "
        "create: new event (title, start, optional end; default 1 hour, or all day for a date). "
        "update: move or rename an event by event_id (moving the start keeps its length). "
        "delete: asks Nathan to confirm; it is NOT deleted until he types the code shown. "
        "Times: 'YYYY-MM-DDTHH:MM' in Nathan's local time, or 'YYYY-MM-DD' for all day. "
        "Never invite people. Report failures honestly; never claim an event exists unless the "
        "tool returned it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "busy", "create", "update", "delete"]},
            "title": {"type": "string", "description": "Event title (create/update)."},
            "start": {"type": "string", "description": "Start (create/update), or range start (list/busy)."},
            "end": {"type": "string", "description": "End (optional), or range end (list/busy)."},
            "description": {"type": "string", "description": "Optional notes on the event."},
            "event_id": {"type": "string", "description": "From list/create (update/delete)."},
        },
        "required": ["action"],
    },
}


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "")
    except Exception:
        return ""


def _who() -> tuple[str, str]:
    chat_name = _session("HERMES_SESSION_CHAT_NAME")
    hashed = [p.strip()[1:] for p in chat_name.split(" / ") if p.strip().startswith("#")]
    channel = (hashed[0] if hashed else chat_name) or _session("HERMES_SESSION_PLATFORM") or "unknown"
    return channel, _session("HERMES_SESSION_USER_NAME") or "unknown"


def _post_audit(text: str) -> None:
    gw = _gateway
    if gw is None:
        return
    try:
        from gateway.config import Platform
        adapter = getattr(gw, "adapters", {}).get(Platform.DISCORD)
    except Exception:
        adapter = None
    client = getattr(adapter, "_client", None)
    loop = getattr(client, "loop", None)
    if client is None or loop is None:
        return

    async def _send():
        try:
            for guild in client.guilds:
                for channel in guild.text_channels:
                    if channel.name == _AUDIT_CHANNEL:
                        await channel.send(text[:1900])
                        return
        except Exception as exc:
            logger.debug("[calendar] audit post failed: %s", exc)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop)
    except Exception as exc:
        logger.debug("[calendar] audit scheduling failed: %s", exc)


def _record(action: str, ok: bool, channel: str, user: str, event: dict | None = None, note: str = "") -> None:
    ev = event or {}
    log({"action": action, "ok": ok, "channel": channel, "user": user, "event_id": ev.get("event_id"),
         "title": ev.get("title"), "start": ev.get("start"), "end": ev.get("end"), "note": note[:300]})
    mark = "✅" if ok else "❌"
    what = f"**{ev.get('title', '?')}** {ev.get('start', '')}" if ev else note
    _post_audit(f"📅 Calendar {action} {mark} `#{channel}` · **{user}** · {what}"[:500])


def _rate_ok() -> bool:
    now = time.time()
    while _writes and now - _writes[0] > 3600:
        _writes.popleft()
    if len(_writes) >= _WRITES_PER_HOUR:
        return False
    _writes.append(now)
    return True


def _handle_calendar(args: dict, **_kwargs) -> str:
    action = str(args.get("action") or "list")
    channel, user = _who()
    try:
        if action == "list":
            out = {"ok": True, "events": _cal.list_events(args.get("start"), args.get("end"))}
        elif action == "busy":
            out = {"ok": True, "busy": _cal.busy(args.get("start"), args.get("end")),
                   "note": "Times Nathan is busy on his main calendar; details are not visible."}
        elif action in ("create", "update", "delete") and not _rate_ok():
            raise CalendarError(f"more than {_WRITES_PER_HOUR} calendar changes in an hour; try later")
        elif action == "create":
            ev = _cal.create(args.get("title"), args.get("start"), args.get("end"), args.get("description") or "")
            _record("create", True, channel, user, ev)
            out = {"ok": True, "created": ev}
        elif action == "update":
            ev = _cal.update(args.get("event_id"), args.get("title"), args.get("start"),
                             args.get("end"), args.get("description"))
            _record("update", True, channel, user, ev)
            out = {"ok": True, "updated": ev}
        elif action == "delete":
            ev = _cal.get_event(args.get("event_id"))
            code = secrets.token_hex(3)
            _pending[code] = {**ev, "expires": time.time() + _CONFIRM_SECS, "channel": channel, "user": user}
            out = {"ok": True, "deleted": False, "needs_confirmation": True,
                   "tell_nathan": f"To delete '{ev['title']}' ({ev['start']}), type: /calendar-confirm {code} "
                                  "(valid 30 minutes). It has NOT been deleted yet."}
        else:
            raise CalendarError(f"unknown action '{action}'")
    except CalendarError as exc:
        if action in ("create", "update"):
            _record(action, False, channel, user, note=str(exc))
        out = {"ok": False, "error": str(exc)}
    except Exception as exc:  # never crash the agent loop
        logger.warning("[calendar] %s failed: %s", action, exc)
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return json.dumps(out, ensure_ascii=False)


def _cmd_confirm(raw_args: str = "") -> str:
    code = raw_args.strip().lower()
    now = time.time()
    for k in [k for k, v in _pending.items() if v["expires"] < now]:
        _pending.pop(k)
    item = _pending.pop(code, None)
    if not item:
        return "❌ No pending calendar delete with that code (codes expire after 30 minutes)."
    try:
        _cal.delete(item["event_id"])
    except CalendarError as exc:
        _record("delete", False, item["channel"], item["user"], item, str(exc))
        return f"❌ Delete failed: {exc}"
    _record("delete", True, item["channel"], item["user"], item, "confirmed by /calendar-confirm")
    return f"🗑️ Deleted **{item['title']}** ({item['start']}) from the ProxAI calendar."


def _cmd_calendar(raw_args: str = "") -> str:
    try:
        events = _cal.list_events()
    except CalendarError as exc:
        return f"❌ Calendar: {exc}"
    if not events:
        return "📅 Nothing on the ProxAI calendar in the next 7 days."
    return "📅 **ProxAI calendar, next 7 days**\n" + "\n".join(
        f"• {e['start']} · {e['title']}" for e in events[:25])


async def _on_pre_gateway_dispatch(event=None, gateway=None, **_kwargs):
    """Remember the gateway so changes can be posted to #bot-audit-log. Never changes the event."""
    global _gateway
    source = getattr(event, "source", None)
    if source is not None and gateway is not None \
            and getattr(source.platform, "value", source.platform) == "discord":
        _gateway = gateway
    return None


def register(ctx) -> None:
    ctx.register_tool(name="calendar", toolset="proxai_calendar", schema=SCHEMA,
                      handler=_handle_calendar, emoji="📅",
                      description="Events on ProxAI's own Google calendar (busy times from Nathan's)")
    ctx.register_command("calendar", _cmd_calendar, description="ProxAI calendar: next 7 days")
    ctx.register_command("calendar-confirm", _cmd_confirm,
                         description="Confirm a calendar delete Hermes asked for", args_hint="<code>")
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
