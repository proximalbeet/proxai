"""
Plugin-written captures for the tier1 capture channels (Phase 6).

The model never decides where a capture goes. This code saves the note with
plain rules, then tells the model the note is already saved so it only writes
the reply.

  #brain-dump      every message → 05-Brain-Dump/YYYY-MM-DD-HHmm-<slug>.md
                   thread follow-ups are added to the thread's note
                   (follow-ups ending in "?" are questions and aren't saved)
  #daily-goals     a message with goals → _inbox/ note, type: daily, date:
                   (the refiler merges it into 00-Daily/<date>.md)
                   messages ending in "?" are questions and aren't saved
  #focus-sessions  "start <task>" starts a session, "end/stop/done <what got
                   done>" saves a focus-log note to _inbox/ (the refiler moves
                   it to 06-Focus-Logs/)

Notes are written on the host (the gateway's side), never overwrite an
existing file, and only ever land in 05-Brain-Dump/ or _inbox/.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ALLOWED_FOLDERS = ("05-Brain-Dump", "_inbox")
STATE_DIR = Path(os.environ.get("PROXAI_STATE_DIR", Path.home() / ".local/state/proxai"))

_MENTION_RE = re.compile(r"<@!?\d+>")
_CHANNEL_RE = re.compile(r"<#(\d+)>")
_SENTENCE_RE = re.compile(r"(?<=[.!])\s+")
_LIST_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(?:\[[ xX]\]\s*)?")
_START_RE = re.compile(r"^(?:start(?:ing)?|begin)\b[\s:,-]*(.*)$", re.I | re.S)
_END_RE = re.compile(r"^(?:end(?:ed)?|stop(?:ped)?|done|finish(?:ed)?)\b[\s:,.!-]*(.*)$", re.I | re.S)


@dataclass
class Capture:
    """What happened to one message. ``path`` is vault-relative."""
    saved: bool
    path: str = ""
    summary: str = ""   # one line for the model ("saved 3 goals to ...")
    notice: str = ""    # optional message the plugin posts itself


# What the Discord adapter puts in event.text for a message with no text (e.g. a voice note).
EMPTY_PLACEHOLDER = "(The user sent a message with no text content)"


def clean_text(text: str) -> str:
    text = _MENTION_RE.sub("", text or "").strip()
    return "" if text == EMPTY_PLACEHOLDER else text


def resolve_channel_mentions(text: str, lookup) -> str:
    """Turn Discord's ``<#123>`` into ``#channel-name`` (``lookup(id)`` → name or None)."""
    def name(m):
        try:
            found = lookup(int(m.group(1)))
        except Exception:
            found = None
        return f"#{found}" if found else "#channel"
    return _CHANNEL_RE.sub(name, text or "")


def slugify(text: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    slug = "-".join(words)[:limit].strip("-")
    return slug or "note"


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is valid YAML


class CaptureWriter:
    def __init__(self, vault: Path, state_dir: Optional[Path] = None):
        self.vault = Path(vault).expanduser().resolve()
        # Read STATE_DIR at call time (not as a default argument) so tests can point it at a temp dir.
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR

    # ---- file helpers ----------------------------------------------------

    def _folder(self, name: str) -> Path:
        if name not in ALLOWED_FOLDERS:
            raise ValueError(f"captures may not write to {name}")
        folder = (self.vault / name).resolve()
        if folder.parent != self.vault:
            raise ValueError(f"{name} is outside the vault")
        folder.mkdir(exist_ok=True)
        return folder

    def _write_new(self, folder: str, filename: str, text: str) -> str:
        """Create a new file (never overwrites); returns the vault-relative path."""
        base = self._folder(folder)
        stem, suffix = filename[:-3], ".md"
        n = 1
        while True:
            path = base / (f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}")
            try:
                with open(path, "x", encoding="utf-8") as f:
                    f.write(text)
                return f"{folder}/{path.name}"
            except FileExistsError:
                n += 1

    # ---- #brain-dump -----------------------------------------------------

    def _find_thread_note(self, anchor_id: str) -> Optional[Path]:
        folder = self._folder("05-Brain-Dump")
        needle = f"discord_message: {_yaml_str(anchor_id)}"
        for path in folder.glob("*.md"):
            try:
                with open(path, encoding="utf-8") as f:
                    head = f.read(2048)
            except OSError:
                continue
            if needle in head:
                return path
        return None

    def brain_dump(self, text: str, message_id: str, thread_id: str, now: dt.datetime,
                   voice: bool = False) -> Capture:
        text = clean_text(text)
        if not text:
            return Capture(False)
        if thread_id:
            note = self._find_thread_note(thread_id)
            if note is not None:
                if text.endswith("?"):
                    return Capture(False, summary="question in a brain-dump thread, not saved")
                old = note.read_text(encoding="utf-8")
                new = re.sub(r"^updated: .*$", f"updated: {now:%Y-%m-%d}", old, count=1, flags=re.M)
                label = "Follow-up (voice)" if voice else "Follow-up"
                new = new.rstrip("\n") + f"\n\n**{label} {now:%H:%M}:** {text}\n"
                note.write_text(new, encoding="utf-8")
                rel = f"05-Brain-Dump/{note.name}"
                return Capture(True, rel, f"added this follow-up to {rel}")
        anchor = thread_id or message_id or ""
        title = text.splitlines()[0][:80]
        body = (
            "---\n"
            "type: note\n"
            "area: knowledge\n"
            "status: inbox\n"
            f"title: {_yaml_str(title)}\n"
            f"created: {now:%Y-%m-%d}\n"
            f"updated: {now:%Y-%m-%d}\n"
            "source: discord #brain-dump\n"
            + ("input: voice\n" if voice else "")
            + f"discord_message: {_yaml_str(anchor)}\n"
            + ("tags: [brain-dump, voice]\n" if voice else "tags: [brain-dump]\n")
            + f"---\n\n{text}\n"
        )
        rel = self._write_new("05-Brain-Dump", f"{now:%Y-%m-%d-%H%M}-{slugify(title, 30)}.md", body)
        return Capture(True, rel, f"saved this brain dump as {rel}")

    # ---- #daily-goals ----------------------------------------------------

    @staticmethod
    def parse_goals(text: str) -> list[str]:
        goals = []
        for line in clean_text(text).splitlines():
            item = _LIST_RE.sub("", line).strip()
            item = re.sub(r"\*\*(.*?)\*\*", r"\1", item).strip()
            if item and not re.fullmatch(r"(?i)(today'?s )?goals?( for today)?:?", item):
                goals.append(item)
        return goals

    def daily_goals(self, text: str, now: dt.datetime, voice: bool = False) -> Capture:
        text = clean_text(text)
        if not text or text.endswith("?"):
            return Capture(False, summary="a question, not goals, so nothing was saved")
        if voice and len(text.splitlines()) == 1:
            # A transcript is one paragraph: one goal per spoken sentence.
            text = "\n".join(s.strip().rstrip(".!") for s in _SENTENCE_RE.split(text) if s.strip())
        goals = self.parse_goals(text)
        if not goals:
            return Capture(False)
        day = now.date().isoformat()
        body = (
            "---\n"
            "type: daily\n"
            "area: personal\n"
            "status: inbox\n"
            f"date: {day}\n"
            f"title: {_yaml_str('Daily goals ' + day)}\n"
            f"created: {day}\n"
            "source: discord #daily-goals\n"
            + ("input: voice\n" if voice else "")
            + "---\n\n"
            + "".join(f"- [ ] {g}\n" for g in goals)
        )
        rel = self._write_new("_inbox", f"{day}-daily-goals-{now:%H%M%S}.md", body)
        return Capture(True, rel, f"saved {len(goals)} goal(s) for {day} as {rel}; "
                                  f"the refiler adds them to 00-Daily/{day}.md")

    # ---- #focus-sessions -------------------------------------------------

    def _session_file(self) -> Path:
        return self.state_dir / "focus-session.json"

    def _load_session(self) -> Optional[dict]:
        try:
            return json.loads(self._session_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_session(self, data: Optional[dict]) -> None:
        path = self._session_file()
        if data is None:
            path.unlink(missing_ok=True)
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    def _write_focus_log(self, session: dict, end: dt.datetime, done: str) -> tuple[str, int]:
        start = dt.datetime.fromisoformat(session["start"])
        minutes = max(0, round((end - start).total_seconds() / 60))
        task = session.get("task") or "focus session"
        day = start.date().isoformat()
        body = (
            "---\n"
            "type: focus-log\n"
            "area: knowledge\n"
            "status: inbox\n"
            f"date: {day}\n"
            f'day: "[[{day}]]"\n'
            f"title: {_yaml_str('Focus session — ' + task)}\n"
            f"task: {_yaml_str(task)}\n"
            f"start: {start.isoformat(timespec='seconds')}\n"
            f"end: {end.isoformat(timespec='seconds')}\n"
            f"duration: {minutes}\n"
            f"created: {day}\n"
            "source: discord #focus-sessions\n"
            "tags: [focus-log]\n"
            "---\n\n"
            f"**Task:** {task}\n\n"
            f"**What got done:** {done or '(not given)'}\n"
        )
        rel = self._write_new("_inbox", f"{day}-{start:%H%M}-{slugify(task, 50)}.md", body)
        return rel, minutes

    def focus_session(self, text: str, now: dt.datetime) -> Capture:
        text = clean_text(text)
        m_start, m_end = _START_RE.match(text), _END_RE.match(text)
        if m_start:
            task = m_start.group(1).strip().splitlines()[0].strip(" .") if m_start.group(1).strip() else ""
            previous = self._load_session()
            note = ""
            if previous:
                rel, mins = self._write_focus_log(previous, now, "(ended when a new session started)")
                note = f" The previous session ({previous.get('task') or 'untitled'}, {mins} min) was saved as {rel}."
            self._save_session({"task": task, "start": now.isoformat(timespec="seconds")})
            return Capture(True, "", f"started a focus session at {now:%H:%M} on: {task or 'untitled'}.{note} "
                                     "Nothing is written to the vault until it ends")
        if m_end:
            session = self._load_session()
            if not session:
                return Capture(False, summary="Nathan tried to end a session but none is running, so nothing was saved")
            rel, minutes = self._write_focus_log(session, now, m_end.group(1).strip())
            self._save_session(None)
            return Capture(True, rel, f"ended the session ({minutes} min) and saved it as {rel}; "
                                      "the refiler moves it to 06-Focus-Logs/")
        return Capture(False)
