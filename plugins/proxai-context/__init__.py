"""
The real conversation size for ProxMobile's token counter.

A reply's usage from the API server adds up every model call in the turn (a turn with 4 tool steps
reads the conversation 4 times), so the phone can't tell how big the conversation is. This
``post_api_request`` hook saves, per session, the prompt size of the latest model call (what the
model actually read, cache included) and the context window. ProxMobile's apps server serves it
at /apps/v1/pc/context.

File: ~/.local/state/proxai/context/sessions.json  {"latest": {...}, "sessions": {id: {...}}}
Only phone (api_server) sessions; no message text is stored.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

STATE = Path(os.getenv("PROXAI_CONTEXT_DIR", str(Path.home() / ".local/state/proxai/context")))
KEEP = 30
_lock = threading.Lock()


def _read() -> dict:
    try:
        d = json.loads((STATE / "sessions.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record(session_id: str, prompt_tokens: int, context_length, model: str, call) -> dict:
    rec = {"session": session_id, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "prompt_tokens": prompt_tokens,
           "context_length": int(context_length) if isinstance(context_length, (int, float)) else None,
           "model": str(model or ""), "call": call if isinstance(call, int) else None}
    with _lock:
        d = _read()
        sessions = d.get("sessions") if isinstance(d.get("sessions"), dict) else {}
        sessions[session_id] = rec
        if len(sessions) > KEEP:
            newest = sorted(sessions.values(), key=lambda r: r.get("time", ""))[-KEEP:]
            sessions = {r["session"]: r for r in newest}
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = STATE / f".sessions.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"latest": rec, "sessions": sessions}), encoding="utf-8")
        os.replace(tmp, STATE / "sessions.json")
    return rec


def _on_post_api_request(session_id="", platform="", model="", usage=None, context_length=None,
                         api_call_count=None, **_kwargs):
    if platform != "api_server" or not session_id or not isinstance(usage, dict):
        return None
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if prompt > 0:
        try:
            record(session_id, prompt, context_length, model, api_call_count)
        except OSError:
            pass
    return None


def register(ctx) -> None:
    ctx.register_hook("post_api_request", _on_post_api_request)
