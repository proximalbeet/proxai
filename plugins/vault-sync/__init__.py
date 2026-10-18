"""
Vault sync status for the ProxAI profile (checklist Phase 8).

The sync itself runs on the host (``vault-sync/vault_sync.py`` from the
``proxai-vault-sync`` systemd user timer). Hermes never runs git: this plugin only

* ``vault_sync`` tool: ``status`` reads ``~/.local/state/proxai/vault-sync.json``;
  ``sync_now`` asks systemd to start the service (fixed command, no arguments from the model).
* ``/vault-sync [now]`` slash command: same, without a model in the loop.
* A watcher thread that posts once to #bot-audit-log when a sync gets stuck (conflict,
  blocked repo, repeated failure) and once when it recovers.

Private folders are already masked in the state file by vault_sync.py.
Which channels may call the tool is decided by the discord-routing guard (tool_policy).
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_DIR = Path.home() / ".local/state/proxai"
_STATE = _STATE_DIR / "vault-sync.json"
_ALERTED = _STATE_DIR / "vault-sync-alerted"
_UNIT = "proxai-vault-sync.service"
_AUDIT_CHANNEL = "bot-audit-log"
_WATCH_SECS = 60
_STALE_SECS = 45 * 60  # no successful sync for this long → worth mentioning

_gateway = None
_watcher_started = False

SCHEMA = {
    "name": "vault_sync",
    "description": (
        "Check or trigger the ProxVault git sync between this desktop and Nathan's laptop. "
        "The sync runs on the host every 10 minutes; you cannot run git yourself. "
        "action=status: when it last synced and whether it is stuck (e.g. the laptop and desktop "
        "edited the same lines of a note, which Nathan must fix by hand). "
        "action=sync_now: start a sync right away (use when Nathan asks, or after saving notes he "
        "wants on the laptop now). Never claim a sync happened unless status shows it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "sync_now"]},
        },
        "required": ["action"],
    },
}

_LABELS = {
    "ok": "in sync", "skipped": "skipped (a note was being written; retries next run)",
    "locked": "skipped (the refiler was busy; retries next run)",
    "retry": "laptop pushed at the same moment; retries next run",
    "conflict": "PAUSED: conflict, needs Nathan", "blocked": "PAUSED: repo needs a hand fix",
    "error": "failed (retries next run)",
}


def _read_state() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _age(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        secs = time.time() - time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return ts
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    if secs < 172800:
        return f"{secs / 3600:.1f} h ago"
    return f"{int(secs // 86400)} days ago"


def _clean(text: str) -> str:
    # Hermes treats any tool result containing "error" as a failed call; this is a status report.
    return str(text).replace("error", "problem").replace("Error", "Problem")


def _status() -> dict:
    s = _read_state()
    if not s:
        return {"ok": True, "state": "no sync has run yet (timer not installed?)"}
    out = {
        "ok": True,
        "state": _LABELS.get(s.get("result"), s.get("result")),
        "detail": _clean(s.get("message", "")),
        "last_run": f"{s.get('time')} ({_age(s.get('time'))})",
        "last_successful_sync": f"{s.get('last_ok')} ({_age(s.get('last_ok'))})",
    }
    if s.get("conflict_files"):
        out["conflicting_notes"] = s["conflict_files"]
        out["how_to_fix"] = ("On the desktop: cd ~/Documents/ProxVault && git rebase origin/main, fix the "
                             "marked lines, git add <note>, git rebase --continue. The next sync pushes it.")
    if s.get("since"):
        out["stuck_since"] = s["since"]
    if s.get("warnings"):
        out["warnings"] = [_clean(w) for w in s["warnings"]]
    return out


def _sync_now() -> dict:
    p = subprocess.run(["systemctl", "--user", "start", "--no-block", _UNIT],
                       capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout).strip()[:300] or f"systemctl exit {p.returncode}"}
    return {"ok": True, "started": True,
            "note": "Sync started on the host; it usually takes under a minute. Check action=status after."}


def _handle_vault_sync(args: dict, **_kwargs) -> str:
    action = str(args.get("action") or "status")
    try:
        result = _sync_now() if action == "sync_now" else _status()
    except Exception as exc:  # never crash the agent loop
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return json.dumps(result, ensure_ascii=False)


def _command(raw_args: str = "") -> str:
    if raw_args.strip().lower() in ("now", "run", "sync"):
        r = _sync_now()
        return "🔄 Vault sync started." if r.get("ok") else f"❌ Could not start the sync: {r.get('error')}"
    s = _status()
    lines = [f"🔄 **Vault sync:** {s['state']}"]
    for key in ("detail", "last_run", "last_successful_sync", "stuck_since"):
        if s.get(key):
            lines.append(f"• {key.replace('_', ' ')}: {s[key]}")
    if s.get("conflicting_notes"):
        lines.append("• notes: " + ", ".join(f"`{n}`" for n in s["conflicting_notes"]))
        lines.append("• fix: " + s["how_to_fix"])
    for w in s.get("warnings", []):
        lines.append(f"• ⚠️ {w}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- alerts

def _post_audit(text: str) -> bool:
    gw = _gateway
    if gw is None:
        return False
    try:
        from gateway.config import Platform
        adapter = getattr(gw, "adapters", {}).get(Platform.DISCORD)
    except Exception:
        adapter = None
    client = getattr(adapter, "_client", None)
    loop = getattr(client, "loop", None)
    if client is None or loop is None:
        return False

    async def _send():
        for guild in client.guilds:
            for channel in guild.text_channels:
                if channel.name == _AUDIT_CHANNEL:
                    await channel.send(text[:1900])
                    return True
        return False

    try:
        return bool(asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=30))
    except Exception as exc:
        logger.debug("[vault-sync] audit post failed: %s", exc)
        return False


def _alert_text(s: dict) -> tuple[str, str] | None:
    """What to post for this state, keyed so each problem is announced once."""
    key = s.get("alert_key")
    if key:
        msg = f"⚠️ **Vault sync paused** ({s.get('result')}): {s.get('message', '')}"
        if s.get("conflict_files"):
            msg += "\nNotes: " + ", ".join(f"`{n}`" for n in s["conflict_files"])
        return key, msg + "\nNothing was overwritten. `/vault-sync` for details."
    if s.get("result") == "ok":
        return "ok", "✅ Vault sync is working again."
    last_ok = s.get("last_ok")
    if last_ok and time.time() - time.mktime(time.strptime(last_ok[:19], "%Y-%m-%dT%H:%M:%S")) > _STALE_SECS:
        return f"stale:{last_ok}", f"⚠️ Vault hasn't synced since {last_ok} (last: {s.get('message', '')})."
    return None


def _watch() -> None:
    try:
        last = _ALERTED.read_text().strip()
    except OSError:
        last = "ok"  # first start: only announce problems
    while True:
        time.sleep(_WATCH_SECS)
        try:
            alert = _alert_text(_read_state())
            if not alert:
                continue
            key, text = alert
            if key == last or (key == "ok" and last == "ok"):
                continue
            if _post_audit(text):
                last = key
                _ALERTED.write_text(key)
        except Exception as exc:
            logger.debug("[vault-sync] watcher: %s", exc)


async def _on_pre_gateway_dispatch(event=None, gateway=None, **_kwargs):
    """Remember the gateway so the watcher can post to #bot-audit-log. Never changes the event."""
    global _gateway, _watcher_started
    source = getattr(event, "source", None)
    if source is not None and gateway is not None \
            and getattr(source.platform, "value", source.platform) == "discord":
        _gateway = gateway
        if not _watcher_started:
            _watcher_started = True
            threading.Thread(target=_watch, name="vault-sync-watch", daemon=True).start()
    return None


def register(ctx) -> None:
    ctx.register_tool(name="vault_sync", toolset="vault_sync", schema=SCHEMA,
                      handler=_handle_vault_sync, emoji="🔄",
                      description="Vault git sync status / start a sync (host timer)")
    ctx.register_command("vault-sync", _command,
                         description="Vault git sync status; '/vault-sync now' starts a sync",
                         args_hint="[now]")
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
