"""The PC's notice feed for Nathan's phone (decided 2026-10-23): scheduled-job results, "your app is
ready", "a wiki update is waiting"... ProxMobile reads it through the apps server
(/apps/v1/pc/notices?after=N) and shows each one as a notification.

    notices.post("📱 Pong is ready", "Install it from the Apps tab.", kind="app")

File: ~/.local/state/proxai/notices/notices.jsonl, one JSON object per line with a rising id.
Other plugins import this file by path (it isn't a plugin itself).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

MAX_TEXT = 1500
KEEP = 500


def feed() -> Path:
    base = Path(os.getenv("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser()
    return base / "notices" / "notices.jsonl"


def post(title: str, text: str = "", kind: str = "info") -> int | None:
    """Append a notice; returns its id (None if it couldn't be written). Never raises."""
    try:
        f = feed()
        f.parent.mkdir(parents=True, exist_ok=True)
        with (f.parent / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
            last = 0
            for line in reversed(lines):
                try:
                    last = int(json.loads(line)["id"])
                    break
                except (ValueError, KeyError, TypeError):
                    continue
            rec = {"id": last + 1, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": str(kind)[:20],
                   "title": str(title).strip()[:120], "text": str(text or "").strip()[:MAX_TEXT]}
            lines = (lines + [json.dumps(rec, ensure_ascii=False)])[-KEEP:]
            tmp = f.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, f)
            return rec["id"]
    except OSError:
        return None


def after(notice_id: int = 0, limit: int = 50) -> list[dict]:
    try:
        lines = feed().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if int(rec.get("id", 0)) > notice_id:
            out.append(rec)
    return out[-limit:]
