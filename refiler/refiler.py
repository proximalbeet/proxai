#!/usr/bin/env python3
"""ProxAI vault refiler.

Files notes that Hermes drops in `_inbox/` into their vault folder, using the
`type` + `area` frontmatter and the SOUL.md filing table. Runs on the host
(outside the Hermes container), as a systemd user timer.

- `type: daily` notes are merged into that day's `00-Daily/<date>.md` under
  "## ✅ Tasks" (created from `_templates/daily-note.md` if missing).
- `type: focus-log` notes get `date` + `day` fields and a dated filename,
  then move to `06-Focus-Logs/`.
- Anything it can't place goes to `_needs-review/`. It never writes to
  `01-Personal/` or `07-Security/`.
- Every action is logged to ~/.local/state/proxai/refiler.log, and every
  note it changes or removes is backed up to ~/.local/state/proxai/refiled/.

Dry run by default; pass --apply to move files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_VAULT = Path.home() / "Documents" / "ProxVault"
STATE_DIR = Path(os.environ.get("PROXAI_STATE_DIR", Path.home() / ".local/state/proxai"))

BLOCKED = ("01-Personal", "07-Security")
REVIEW = "_needs-review"
# Skip notes modified this recently: Hermes may still be writing them.
MIN_AGE_SECONDS = 60

# (type, area) -> folder. area None = any area.
FILING = {
    ("note", "knowledge"): "04-Knowledge",
    ("note", "career"): "09-Career",
    ("note", "course"): "03-Courses",
    ("task", None): "05-Brain-Dump",
    ("project", None): "02-Projects",
    ("role", None): "08-Roles",
    ("reference", "knowledge"): "04-Knowledge",
    ("focus-log", None): "06-Focus-Logs",
}

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*\S)\s*$")
TASKS_HEADING = "## ✅ Tasks"


class Refiler:
    def __init__(self, vault: Path, apply: bool, min_age: int = MIN_AGE_SECONDS):
        self.vault = vault.resolve()
        self.inbox = self.vault / "_inbox"
        self.apply = apply
        self.min_age = min_age
        self.log_path = STATE_DIR / "refiler.log"
        self.backup_dir = STATE_DIR / "refiled"
        self.today = dt.date.today()

    # ---- helpers ---------------------------------------------------------

    def log(self, action: str, src: Path, dst: Path | str = "-", reason: str = "") -> None:
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        rel_src = self._rel(src)
        rel_dst = self._rel(dst) if isinstance(dst, Path) else dst
        mode = "" if self.apply else "DRY-RUN "
        line = f"{stamp} {mode}{action} {rel_src} -> {rel_dst}" + (f" ({reason})" if reason else "")
        print(line)
        if self.apply:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _rel(self, p: Path) -> str:
        try:
            return str(p.resolve().relative_to(self.vault))
        except ValueError:
            return str(p)

    def _safe_dest(self, folder: str) -> Path:
        dest = (self.vault / folder).resolve()
        if dest.parent != self.vault or folder.split("/")[0] in BLOCKED:
            raise RuntimeError(f"refusing destination {folder}")
        return dest

    def _unique(self, path: Path) -> Path:
        if not path.exists():
            return path
        n = 2
        while True:
            cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
            if not cand.exists():
                return cand
            n += 1

    def _backup(self, src: Path) -> None:
        if not self.apply:
            return
        day_dir = self.backup_dir / self.today.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, self._unique(day_dir / src.name))

    def _move(self, src: Path, folder: str, name: str | None = None, text: str | None = None,
              reason: str = "") -> None:
        dest_dir = self._safe_dest(folder)
        dest = self._unique(dest_dir / (name or src.name))
        self.log("MOVE", src, dest, reason)
        if not self.apply:
            return
        dest_dir.mkdir(exist_ok=True)
        if text is not None:
            self._backup(src)
            dest.write_text(text, encoding="utf-8")
            src.unlink()
        else:
            shutil.move(str(src), str(dest))

    # ---- frontmatter -----------------------------------------------------

    @staticmethod
    def parse(text: str) -> tuple[dict[str, str] | None, str, str]:
        """Return (fields, frontmatter block, body). Only top-level scalar keys are read."""
        m = FM_RE.match(text)
        if not m:
            return None, "", text
        fields: dict[str, str] = {}
        for line in m.group(1).splitlines():
            km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if km:
                fields[km.group(1).lower()] = km.group(2).strip().strip("'\"")
        return fields, m.group(1), text[m.end():]

    @staticmethod
    def _date_from(*values: str) -> dt.date | None:
        for v in values:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", v or "")
            if m:
                try:
                    return dt.date.fromisoformat(m.group(1))
                except ValueError:
                    pass
        return None

    # ---- main ------------------------------------------------------------

    def run(self) -> int:
        if not self.inbox.is_dir():
            print(f"no inbox at {self.inbox}", file=sys.stderr)
            return 1
        now = dt.datetime.now().timestamp()
        for src in sorted(self.inbox.iterdir()):
            if not src.is_file() or src.suffix != ".md" or src.name.startswith("."):
                continue  # subfolders like _audit-test/ are left alone
            if now - src.stat().st_mtime < self.min_age:
                self.log("SKIP", src, reason="modified < 60 s ago")
                continue
            try:
                self.file_one(src)
            except Exception as e:  # one bad note must not stop the rest
                self.log("ERROR", src, reason=str(e))
        return 0

    def file_one(self, src: Path) -> None:
        text = src.read_text(encoding="utf-8")
        fields, _, _ = self.parse(text)
        if not fields or not fields.get("type"):
            self._move(src, REVIEW, reason="no frontmatter or no type")
            return
        ntype = fields["type"].lower()
        area = fields.get("area", "").lower()

        if ntype == "daily":
            self.merge_daily(src, text, fields)
            return
        if area == "personal":
            self._move(src, REVIEW, reason="area: personal is never auto-filed")
            return
        if ntype == "focus-log":
            self.file_focus_log(src, text, fields)
            return
        folder = FILING.get((ntype, area)) or FILING.get((ntype, None))
        if not folder:
            self._move(src, REVIEW, reason=f"no filing rule for type={ntype} area={area or '-'}")
            return
        self._move(src, folder, reason=f"type={ntype} area={area or '-'}")

    # ---- focus logs ------------------------------------------------------

    def file_focus_log(self, src: Path, text: str, fields: dict[str, str]) -> None:
        when = self._date_from(fields.get("date", ""), fields.get("start", ""),
                               fields.get("created", ""), src.name) or self.today
        hhmm = "0000"
        tm = re.search(r"T(\d{2}):?(\d{2})", fields.get("start", ""))
        if tm:
            hhmm = tm.group(1) + tm.group(2)
        task = fields.get("task") or fields.get("title") or src.stem
        task = re.sub(r"^focus session\s*[—–-]\s*", "", task, flags=re.I)
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:50] or "session"
        name = f"{when.isoformat()}-{hhmm}-{slug}.md"

        new_text = text
        add = []
        if "date" not in fields:
            add.append(f"date: {when.isoformat()}")
        if "day" not in fields:
            add.append(f'day: "[[{when.isoformat()}]]"')
        if add:
            new_text = re.sub(r"\A---\s*\n", "---\n" + "\n".join(add) + "\n", text, count=1)
        self._move(src, "06-Focus-Logs", name=name,
                   text=new_text if new_text != text or name != src.name else None,
                   reason="type=focus-log" + (" (+" + ", ".join(a.split(":")[0] for a in add) + ")" if add else ""))

    # ---- daily goals -----------------------------------------------------

    def merge_daily(self, src: Path, text: str, fields: dict[str, str]) -> None:
        day = self._date_from(fields.get("date", ""), fields.get("created", ""), src.name) or self.today
        _, _, body = self.parse(text)
        goals = []
        for line in body.splitlines():
            m = LIST_RE.match(line)
            if m:
                item = re.sub(r"\*\*(.*?)\*\*", r"\1", m.group(1)).strip()
                if item:
                    goals.append(item)
        if not goals:
            self._move(src, REVIEW, reason="daily note with no list items")
            return

        daily = self._safe_dest("00-Daily") / f"{day.isoformat()}.md"
        note = daily.read_text(encoding="utf-8") if daily.exists() else self.render_template(day)
        if TASKS_HEADING not in note:
            note = note.rstrip("\n") + f"\n\n{TASKS_HEADING}\n"
        merged, added = self.insert_tasks(note, goals)

        self.log("MERGE", src, daily, f"{added} goal(s) into {TASKS_HEADING}"
                 + ("" if daily.exists() else ", daily note created from template"))
        if not self.apply:
            return
        if daily.exists():
            self._backup(daily)
        daily.parent.mkdir(exist_ok=True)
        daily.write_text(merged, encoding="utf-8")
        self._backup(src)
        src.unlink()

    @staticmethod
    def insert_tasks(note: str, goals: list[str]) -> tuple[str, int]:
        lines = note.split("\n")
        start = lines.index(next(l for l in lines if l.strip() == TASKS_HEADING))
        end = start + 1
        while end < len(lines) and not lines[end].startswith("#") and lines[end].strip() != "---":
            end += 1
        section = lines[start + 1:end]
        # Drop the template's empty placeholder checkboxes ("- [x] ", "- [ ]").
        kept = [l for l in section if not re.fullmatch(r"\s*- \[[ xX]\]\s*", l)]
        existing = {re.sub(r"^\s*- \[[ xX]\]\s*", "", l).strip().lower() for l in kept}
        new = [f"- [ ] {g}" for g in goals if g.lower() not in existing]
        body = [l for l in kept if l.strip()] + new
        out = lines[:start + 1] + body + [""] + lines[end:]
        return "\n".join(out), len(new)

    def render_template(self, day: dt.date) -> str:
        tpl = self.vault / "_templates" / "daily-note.md"
        text = tpl.read_text(encoding="utf-8") if tpl.exists() else f"# {day:%A, %B} {day.day}\n\n{TASKS_HEADING}\n"

        tokens = {"YYYY": "%Y", "dddd": "%A", "MMMM": "%B", "MM": "%m", "DD": "%d"}

        def fill(m: re.Match) -> str:
            d = day + dt.timedelta(days=int(m.group(2) or 0))
            return re.sub(r"YYYY|dddd|MMMM|MM|DD|D",
                          lambda t: str(d.day) if t.group(0) == "D" else d.strftime(tokens[t.group(0)]),
                          m.group(1))

        return re.sub(r"""<%\s*tp\.date\.now\(\s*["']([^"']*)["']\s*(?:,\s*(-?\d+)\s*)?\)\s*%>""",
                      fill, text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", type=Path, default=Path(os.environ.get("PROXAI_VAULT", DEFAULT_VAULT)))
    ap.add_argument("--apply", action="store_true", help="move files (default: dry run)")
    ap.add_argument("--min-age", type=int, default=MIN_AGE_SECONDS, help=argparse.SUPPRESS)
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Shared lock so the refiler and the Phase 8 vault sync never overlap.
    with (STATE_DIR / "vault.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("vault is locked by another job; skipping this run")
            return 0
        return Refiler(args.vault, args.apply, args.min_age).run()


if __name__ == "__main__":
    sys.exit(main())
