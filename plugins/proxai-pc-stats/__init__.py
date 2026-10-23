"""
ProxAI reads the PC's live stats (temperatures, GPU/CPU use, RAM, alerts).

The numbers come from the thermal watch service (ProxMobile repo, proxai-thermal-watch), which
samples every second and keeps ~/.local/state/proxai/thermal-watch/latest.json and alerts.jsonl.
Hermes's sandbox can't see the GPU, so without this the model guesses from /sys.

* ``pre_llm_call`` hook: when Nathan asks about temps, GPU, CPU, RAM, fans, power or the PC's
  health, the current readings are added to his message. The local model doesn't have to find a
  hidden tool (Hermes hides plugin tools behind tool_search).
* ``pc_stats`` tool: the same readings plus recent alerts, for models that do look tools up.
* ``/pc`` command: the readings, straight to the chat.

Read-only: it never runs anything on the GPU and never changes the thermal watch.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

STATE = Path(os.getenv("PROXAI_THERMAL_DIR",
                       str(Path.home() / ".local/state/proxai/thermal-watch")))
STALE_SECS = 120

# Specs: read once from the kernel (no subprocess, nothing touches the GPU).
_specs_cache: str | None = None


def specs() -> str:
    global _specs_cache
    if _specs_cache is not None:
        return _specs_cache
    gpu = cpu = ""
    ram_gb = 0.0
    try:
        for info in Path("/proc/driver/nvidia/gpus").glob("*/information"):
            m = re.search(r"^Model:\s*(.+)$", info.read_text(), re.M)
            if m:
                gpu = m.group(1).strip()
                break
    except OSError:
        pass
    try:
        m = re.search(r"^model name\s*:\s*(.+)$", Path("/proc/cpuinfo").read_text(), re.M)
        cpu = m.group(1).strip() if m else ""
        m = re.search(r"^MemTotal:\s*(\d+)", Path("/proc/meminfo").read_text(), re.M)
        ram_gb = int(m.group(1)) / 1024 / 1024 if m else 0.0
    except OSError:
        pass
    parts = [p for p in (f"GPU {gpu}" if gpu else "", f"CPU {cpu}" if cpu else "",
                         f"{ram_gb:.0f} GB RAM" if ram_gb else "") if p]
    _specs_cache = ", ".join(parts)
    return _specs_cache


def latest() -> dict | None:
    try:
        return json.loads((STATE / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def recent_alerts(hours: float = 24, now: datetime | None = None, limit: int = 5) -> tuple[int, list[dict]]:
    """(count in the window, the last few) from alerts.jsonl."""
    now = now or datetime.now()
    since = now - timedelta(hours=hours)
    try:
        lines = (STATE / "alerts.jsonl").read_text(encoding="utf-8").splitlines()[-500:]
    except OSError:
        return 0, []
    hits = []
    for line in lines:
        try:
            a = json.loads(line)
            if datetime.fromisoformat(a["time"]) >= since:
                hits.append(a)
        except (ValueError, KeyError, TypeError):
            continue
    return len(hits), hits[-limit:]


def _throttle(code) -> str:
    try:
        bits = int(str(code), 16)
    except (TypeError, ValueError):
        return ""
    if bits & (0x20 | 0x40):
        return "thermal throttling"
    if bits & (0x08 | 0x80):
        return "hardware slowdown"
    return ""


def summary(hours: float = 24, now: datetime | None = None) -> str:
    now = now or datetime.now()
    d = latest()
    if not d:
        return ("The PC's thermal watch has no readings (proxai-thermal-watch isn't running?). "
                "Say so; don't guess temperatures.")
    try:
        age = (now - datetime.fromisoformat(d["time"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = 1e9
    lim = d.get("limits") or {}
    gb = lambda mb: f"{(mb or 0) / 1024:.1f}"   # noqa: E731
    n = lambda k: f"{d[k]:.0f}" if isinstance(d.get(k), (int, float)) else "?"   # noqa: E731
    lines = []
    if age > STALE_SECS:
        lines.append(f"WARNING: these readings are {int(age // 60)} min old; the thermal watch may have stopped.")
    lines.append(
        f"GPU: {n('gpu_c')} °C, {n('gpu_w')} W, {n('gpu_use')}% busy, "
        f"VRAM {gb(d.get('vram_mib'))}/{gb(d.get('vram_total_mib'))} GB, fan {n('fan_pct')}%"
        + (f", {_throttle(d.get('throttle'))}" if _throttle(d.get("throttle")) else ""))
    lines.append(f"CPU: {n('cpu_pkg_c')} °C (hottest core {n('cpu_core_max_c')} °C), "
                 f"{n('cpu_use')}% busy")
    lines.append(f"RAM: {gb(d.get('ram_used_mb'))} GB used, {gb(d.get('ram_avail_mb'))} GB free; "
                 f"swap {gb(d.get('swap_mb'))} GB")
    if lim:
        lines.append(f"Alert limits: GPU {lim.get('gpu_warn_c')}/{lim.get('gpu_crit_c')} °C (warn/critical), "
                     f"CPU {lim.get('cpu_warn_c')}/{lim.get('cpu_crit_c')} °C")
    count, last = recent_alerts(hours, now)
    if count:
        lines.append(f"Alerts in the last {hours:g} h: {count}; latest: " +
                     "; ".join(f"{a['time'][11:16]} {a.get('msg', '')}" for a in last[-3:]))
    else:
        lines.append(f"No alerts in the last {hours:g} h.")
    head = f"PC right now ({d.get('time', '?')[11:19]}"
    if specs():
        head += f"; {specs()}"
    return head + "):\n" + "\n".join("- " + line for line in lines)


# ---- hook -------------------------------------------------------------------------------------

_ASK = re.compile(
    r"\b(temps?|temperatures?|thermals?|how hot|overheat\w*|gpu|vram|cpu|fans?|swap|watts?|power draw|"
    r"pc stats|system stats|specs|hardware|"
    r"(pc|computer|rig|desktop|system|machine)('s)? (usage|load|health|status|stats|doing|holding up|running))\b",
    re.I)


def _plain(user_message) -> str:
    if isinstance(user_message, list):
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    return re.sub(r"\[[^\]]*\]", " ", str(user_message or ""))


def _on_pre_llm_call(user_message=None, parent_session_id=None, **_kwargs):
    if parent_session_id:
        return None
    text = _plain(user_message)
    if not _ASK.search(text):
        return None
    return {"context": "[pc-stats] Live readings from the PC's thermal watch (sampled every second). "
                       "Answer from these numbers. The sandbox can't see the GPU, so don't probe /sys, "
                       "sensors or nvidia-smi.\n" + summary()}


# ---- tool + command ---------------------------------------------------------------------------

SCHEMA = {
    "name": "pc_stats",
    "description": ("Nathan's PC right now: GPU/CPU temperatures, GPU/CPU use, VRAM, RAM, swap, fan, "
                    "alert limits and recent heat/RAM alerts, from the thermal watch service. Use it for "
                    "any question about temps, usage or the PC's health; the sandbox can't see the GPU."),
    "parameters": {"type": "object", "properties": {
        "hours": {"type": "number", "description": "How far back to count alerts (default 24)."}}},
}


def _handle(args: dict | None = None, **_kw) -> str:
    try:
        hours = max(1.0, min(24 * 14, float((args or {}).get("hours") or 24)))
    except (TypeError, ValueError):
        hours = 24.0
    return json.dumps({"ok": latest() is not None, "summary": summary(hours)})


def _cmd_pc(raw_args: str = "") -> str:
    return "🌡️ " + summary()


def register(ctx) -> None:
    ctx.register_tool(name="pc_stats", toolset="proxai_pc", schema=SCHEMA, handler=_handle, emoji="🌡️",
                      description="The PC's live temps, usage and alerts")
    ctx.register_command("pc", _cmd_pc, description="The PC's temps, usage and recent alerts")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
