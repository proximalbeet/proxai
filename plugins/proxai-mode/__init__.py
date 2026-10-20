"""
ProxAI server/personal mode (checklist Phase 10): Hermes can tell which mode StormTrooper is in
and, from Nathan's DMs or the phone app, switch it after Nathan confirms.

* ``proxai_mode`` tool: status / check / request / confirm / cancel. The steps a mode runs come
  only from ``mode/modes.toml``; the model can only pick a mode by name.
* Switching is two-step. ``request`` runs the busy check and returns a question for Nathan.
  The ``pre_llm_call`` hook reads Nathan's NEXT message (typed, phone, or a voice-note
  transcript): a plain yes arms the switch, anything else cancels it. ``confirm`` only works
  once armed, so the model can't approve its own request. Requests expire after 3 minutes.
* Allowed only in Discord DMs, the phone app (api_server) and the local CLI. Channels never
  get the tool (it isn't in any discord-routing tool_policy profile), and the plugin checks too.
* The switch itself runs as a separate systemd user job (``systemd-run``), not inside the
  gateway. Each switch is logged to ``~/.local/state/proxai/mode.log`` and posted to
  #bot-audit-log.
* ``/mode`` command: shows the current mode.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_MODE_DIR = Path(os.getenv("PROXAI_MODE_DIR", Path(__file__).resolve().parents[2] / "mode"))
_AUDIT_CHANNEL = "bot-audit-log"
_CONFIRM_SECS = 180
_SWITCH_WAIT_SECS = 60
_ALLOWED_PLATFORMS = {"api_server", "cli"}   # plus Discord DMs

_gateway = None
_pending: dict[str, dict] = {}   # session_id -> {"mode", "created", "confirmed", "where", "who"}


def _engine():
    name = "proxai_mode_engine"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _MODE_DIR / "proxai_mode.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclasses look the module up while it loads
    spec.loader.exec_module(mod)
    return mod


pm = _engine()


def _mode_names() -> list[str]:
    try:
        return list(pm.load_config())
    except Exception:
        return ["server", "personal"]


SCHEMA = {
    "name": "proxai_mode",
    "description": (
        "StormTrooper's SERVER / PERSONAL mode (Nathan's PC). SERVER = he's away: no sleep, screen "
        "locked, RGB off, AI kept warm, bigger local model; it refuses if the PC is busy (game, "
        "render). PERSONAL = he's at the desk: GPU freed for him, Hermes text-only (no voice "
        "replies), normal desktop, RGB on. "
        "status: current mode. check: would a switch to `mode` be allowed now (busy readings). "
        "request: start a switch to `mode`; it returns a question; ask Nathan exactly that and "
        "STOP; the system reads his reply. confirm: only after the system tells you Nathan said "
        "yes. cancel: drop a pending request. Never claim the mode changed unless confirm "
        "returned ok."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "check", "request", "confirm", "cancel"]},
            "mode": {"type": "string", "enum": _mode_names(),
                     "description": "Target mode (check/request)."},
        },
        "required": ["action"],
    },
}


# ---- context ----------------------------------------------------------------------------------

def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        return os.getenv(name, "")


def _where() -> tuple[str, bool]:
    """('dm' | 'api_server' | 'cli' | '#channel' | platform, allowed?)"""
    if _session("HERMES_CRON_SESSION"):
        return "cron", False
    platform = _session("HERMES_SESSION_PLATFORM") or "cli"
    if platform == "discord":
        if _session("HERMES_SESSION_CHAT_TYPE") == "dm":
            return "dm", True
        return "#" + (_session("HERMES_SESSION_CHAT_NAME") or "channel"), False
    return platform, platform in _ALLOWED_PLATFORMS


def _who() -> str:
    return _session("HERMES_SESSION_USER_NAME") or os.getenv("USER", "unknown")


def _session_id() -> str:
    return _session("HERMES_SESSION_ID") or os.getenv("HERMES_SESSION_ID", "")


# ---- audit ------------------------------------------------------------------------------------

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
            logger.debug("[proxai-mode] audit post failed: %s", exc)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop)
    except Exception as exc:
        logger.debug("[proxai-mode] audit scheduling failed: %s", exc)


# ---- what a mode does, in words ---------------------------------------------------------------

_STEP_WORDS = {
    "stop_units": lambda s: "make sure " + ", ".join(u.replace(".service", "") for u in s.get("units", []))
                            + " are off",
    "start_units": lambda s: "start " + ", ".join(u.replace(".service", "") for u in s.get("units", [])),
    "ollama": lambda s: ("keep the local model loaded" if str(s.get("keep_alive")) == "-1"
                         else f"unload the local model after {s.get('keep_alive', '5m')}")
                        + (f", load {', '.join(s['preload'])}" if s.get("preload") else ""),
    "voice_server": lambda s: "voice on and kept loaded" if s.get("enabled", True) else "voice off (text-only)",
    "sleep_block": lambda s: "block sleep" if s.get("on") else "allow sleep",
    "lock_screen": lambda s: "lock the screen",
    "display": lambda s: f"turn the display {s.get('state', 'on')}",
    "rgb": lambda s: "lights off" if s.get("profile") == "off" else f"lights: {s.get('profile')}",
    "command": lambda s: s.get("name") or f"run {s.get('argv', ['?'])[0]}",
}


def _summary(mode: str) -> str:
    try:
        steps = pm.load_config()[mode].get("steps", [])
    except Exception:
        return ""
    return "; ".join(_STEP_WORDS.get(s.get("type"), lambda s: s.get("type", "?"))(s) for s in steps)


# ---- tool -------------------------------------------------------------------------------------

def _status() -> dict:
    st = pm.read_state()
    out = {"ok": True, "mode": st.get("mode", "unknown"), "since": st.get("since"), "by": st.get("by")}
    failed = [f"{s['step']}: {s['detail']}" for s in st.get("steps", []) if not s.get("ok")]
    if failed:
        out["last_switch_problems"] = failed
    return out


def _run_switch(mode: str, by: str) -> dict:
    """Run the switch as its own systemd user job so restarts it triggers can't hit the gateway,
    then wait for its result in mode.log."""
    start = time.time()
    script = str(_MODE_DIR / "proxai_mode.py")
    python = shutil.which("python3") or "/usr/bin/python3"
    argv = ["systemd-run", "--user", "--collect", "--quiet", f"--unit=proxai-mode-switch-{int(start)}",
            python, script, "set", mode, "--by", by]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return {"ok": False, "error": f"couldn't start the switch: {exc}"}
    if p.returncode != 0:
        return {"ok": False, "error": f"couldn't start the switch: {(p.stdout + p.stderr).strip()[:300]}"}
    deadline = start + _SWITCH_WAIT_SECS
    while time.time() < deadline:
        time.sleep(1)
        entry = _last_log_entry(start)
        if entry:
            return entry
    return {"ok": False, "still_running": True,
            "error": f"switch still running after {_SWITCH_WAIT_SECS}s; check proxai_mode status shortly"}


def _last_log_entry(since: float) -> dict | None:
    try:
        if pm.LOG_FILE.stat().st_mtime < since:
            return None
        line = pm.LOG_FILE.read_text().strip().splitlines()[-1]
        return json.loads(line)
    except (OSError, IndexError, ValueError):
        return None


def _handle(args: dict, **_kwargs) -> str:
    action = (args or {}).get("action", "")
    mode = (args or {}).get("mode", "")
    where, allowed = _where()
    sid = _session_id()

    if action == "status":
        return json.dumps(_status())

    if action == "check":
        if mode not in _mode_names():
            return json.dumps({"ok": False, "error": f"mode must be one of {_mode_names()}"})
        r = pm.preflight(mode, pm.load_config())
        return json.dumps({"ok": True, "allowed_now": r["ok"], "reasons": r["reasons"], "readings": r["readings"]})

    if action in ("request", "confirm", "cancel") and not allowed:
        return json.dumps({"ok": False, "error": f"Mode switches only from Nathan's DMs or the phone app, not {where}. "
                                                 "Tell him to ask there."})

    if action == "cancel":
        _pending.pop(sid, None)
        return json.dumps({"ok": True, "cancelled": True})

    if action == "request":
        if mode not in _mode_names():
            return json.dumps({"ok": False, "error": f"mode must be one of {_mode_names()}"})
        if not sid:
            return json.dumps({"ok": False, "error": "no session id; can't track the confirmation"})
        if pm.current_mode() == mode and pm.read_state().get("ok", True):
            return json.dumps({"ok": True, "already": True, "mode": mode,
                               "tell_nathan": f"Already in {mode} mode."})
        # (same mode but the last switch had failed steps: a retry is allowed)
        check = pm.preflight(mode, pm.load_config())
        if not check["ok"]:
            pm._log({"ok": False, "refused": True, "target": mode, "by": f"hermes:{_who()}@{where}",
                     "reasons": check["reasons"], "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
            return json.dumps({"ok": False, "refused": True, "reasons": check["reasons"],
                               "readings": check["readings"],
                               "tell_nathan": f"Can't switch to {mode} mode: the PC is busy ("
                                              + "; ".join(check["reasons"]) + ")."})
        _pending[sid] = {"mode": mode, "created": time.time(), "confirmed": False, "where": where, "who": _who()}
        return json.dumps({
            "ok": True, "needs_confirmation": True,
            "ask_nathan": f"Switch to {mode.upper()} mode? That will: {_summary(mode)}. Say yes to confirm.",
            "instructions": "Ask Nathan this and end your reply. Do NOT call confirm now: the system "
                            "reads his next message and tells you if he said yes.",
        })

    if action == "confirm":
        p = _pending.get(sid)
        if not p:
            return json.dumps({"ok": False, "error": "No pending switch. Use request first."})
        if time.time() - p["created"] > _CONFIRM_SECS:
            _pending.pop(sid, None)
            return json.dumps({"ok": False, "error": "The request expired. Ask again with request."})
        if not p["confirmed"]:
            return json.dumps({"ok": False, "error": "Nathan hasn't said yes yet. Ask him and wait for his reply."})
        _pending.pop(sid, None)
        by = f"hermes:{p['who']}@{p['where']}"
        result = _run_switch(p["mode"], by)
        if result.get("refused"):
            msg = f"🔀 Mode switch to **{p['mode']}** refused (busy) · {by} · " + "; ".join(result.get("reasons", []))
        elif result.get("mode") == p["mode"]:
            bad = [s["step"] for s in result.get("steps", []) if not s.get("ok")]
            msg = f"🔀 Mode → **{p['mode'].upper()}** {'✅' if not bad else '⚠️ ' + ', '.join(bad)} · {by}"
        else:
            msg = f"🔀 Mode switch to **{p['mode']}** ❌ · {by} · {result.get('error', '')}"
        _post_audit(msg)
        return json.dumps({k: v for k, v in result.items() if k in
                           ("ok", "mode", "refused", "reasons", "steps", "error", "still_running")})

    return json.dumps({"ok": False, "error": f"unknown action '{action}'"})


# ---- hooks ------------------------------------------------------------------------------------

_YES = re.compile(
    r"^(?:(?:yes|yeah|yea|yep|yup|ya|sure|ok|okay|confirm|confirmed|approve|approved|affirmative|"
    r"do it|go ahead|go for it|switch it|switch|send it|let'?s go|please|bro|man|ye|yay|now)\b[\s,.!]*)+$")
_NO_WORDS = re.compile(r"\b(no|nah|nope|don'?t|do not|not|wait|cancel|stop|hold on|never)\b")
_MODE_WORDS = re.compile(r"\b(server|personal|user)\s+mode\b|\bmode\b.{0,30}\b(switch|toggle|change)|"
                         r"\b(switch|toggle|change)\b.{0,30}\bmode\b", re.IGNORECASE)


def _plain_text(user_message) -> str:
    if isinstance(user_message, list):   # multimodal content parts
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    text = str(user_message or "")
    text = re.sub(r"\[[^\]]*\]", " ", text)                 # gateway notes like [Voice channel now: ...]
    text = text.lower().replace("’", "'")
    text = re.sub(r"[\"“”*_`~]", " ", text)                  # voice transcripts arrive quoted
    return " ".join(text.split()).strip(" .!,")


def is_yes(user_message) -> bool:
    text = _plain_text(user_message)
    return bool(text) and len(text) <= 60 and not _NO_WORDS.search(text) and bool(_YES.match(text))


def _on_pre_llm_call(session_id=None, user_message=None, parent_session_id=None, **_kwargs):
    p = _pending.get(session_id or "")
    if p and not parent_session_id:
        if time.time() - p["created"] > _CONFIRM_SECS:
            _pending.pop(session_id, None)
            return {"context": f"[proxai-mode] The request to switch to {p['mode']} mode expired; nothing changed."}
        if p["confirmed"]:
            return None
        if is_yes(user_message):
            p["confirmed"] = True
            return {"context": f"[proxai-mode] Nathan confirmed switching to {p['mode']} mode. "
                               "Call proxai_mode with action=confirm now, then report the result."}
        _pending.pop(session_id, None)
        return {"context": f"[proxai-mode] Nathan did not confirm (his reply wasn't a plain yes), so the "
                           f"switch to {p['mode']} mode is cancelled. Don't switch; ask again only if he wants."}
    text = user_message if isinstance(user_message, str) else json.dumps(user_message or "")[:2000]
    if _MODE_WORDS.search(text or ""):
        return {"context": "[proxai-mode] For server/personal mode use the `proxai_mode` tool (find it with "
                           "tool_search if it isn't loaded): status, check, or request a switch."}
    return None


async def _on_pre_gateway_dispatch(event=None, gateway=None, **_kwargs):
    """Remember the gateway so switches can be posted to #bot-audit-log. Never changes the event."""
    global _gateway
    source = getattr(event, "source", None)
    if source is not None and gateway is not None \
            and getattr(source.platform, "value", source.platform) == "discord":
        _gateway = gateway
    return None


def _cmd_mode(raw_args: str = "") -> str:
    st = _status()
    line = f"🔀 **Mode: {str(st['mode']).upper()}**"
    if st.get("since"):
        line += f" since {st['since'].replace('T', ' ')} (by {st.get('by', '?')})"
    if st.get("last_switch_problems"):
        line += "\n⚠️ " + "\n⚠️ ".join(st["last_switch_problems"])
    return line + "\nSwitch: ask me in DMs or on the phone (I'll ask you to confirm), or run `proxai-mode set <mode>`."


def register(ctx) -> None:
    ctx.register_tool(name="proxai_mode", toolset="proxai_mode", schema=SCHEMA, handler=_handle,
                      emoji="🔀", description="StormTrooper server/personal mode: status, check, switch (Nathan confirms)")
    ctx.register_command("mode", _cmd_mode, description="Show StormTrooper's server/personal mode")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
