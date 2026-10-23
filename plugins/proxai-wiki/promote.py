#!/usr/bin/env python3
"""Copies a wiki update from the workspace into the vault's 10-Wiki. The only writer of 10-Wiki for
the wiki synthesizer; run by the plugin as its own sandboxed systemd user job (it sees just
10-Wiki, the read-only workspace and its state folder; no network).

    promote.py <request.json>
    request: {"id", "changes": [["A"|"M"|"D", "concepts/x.md"], ...]}
    result:  <state>/wiki/results/<id>.json  {"ok", "written", "deleted", "backup", "error"}

Rules: only Markdown files, relative paths inside 10-Wiki, nothing starting with "." (the
workspace's .git/.proxai), no symlinks, at most MAX_FILES per update. Every file it replaces or
deletes is backed up first. Takes the shared vault lock (refiler, vault sync, vault writes).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path, PurePosixPath

MAX_FILES = 300
LOCK_WAIT_SECS = 30


def vault_dir() -> Path:
    return Path(os.environ.get("PROXAI_VAULT", "~/Documents/ProxVault")).expanduser()


def state_dir() -> Path:
    return Path(os.environ.get("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser()


def work_dir() -> Path:
    return Path(os.environ.get("PROXAI_WIKI_WORK", "~/dev/proxai/wiki-work")).expanduser()


class Refused(Exception):
    pass


def clean(rel: str) -> str:
    p = str(rel or "").strip()
    parts = PurePosixPath(p).parts
    if not p or p.startswith("/") or not parts or any(x in ("", ".", "..") or x.startswith(".") for x in parts):
        raise Refused(f"not a wiki path: {rel!r}")
    if not p.lower().endswith(".md"):
        raise Refused(f"only Markdown files: {rel!r}")
    return "/".join(parts)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _lock():
    f = (state_dir() / "vault.lock").open("w")
    deadline = time.monotonic() + LOCK_WAIT_SECS
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() > deadline:
                f.close()
                raise Refused("the vault is busy (refiler or sync running); try again in a minute")
            time.sleep(0.5)


def apply(req: dict) -> dict:
    wiki, work = vault_dir() / "10-Wiki", work_dir()
    changes = req.get("changes") or []
    if len(changes) > MAX_FILES:
        raise Refused(f"{len(changes)} files is more than one update may change ({MAX_FILES})")
    plan = []
    for status, rel in changes:
        rel = clean(rel)
        status = str(status)[:1].upper()
        if status not in ("A", "M", "D"):
            raise Refused(f"unknown change {status!r} for {rel}")
        src, dst = work / rel, wiki / rel
        if status != "D":
            if src.is_symlink() or not src.is_file() or not _inside(src, work):
                raise Refused(f"{rel} isn't a plain file in the workspace")
        if dst.is_symlink() or (dst.exists() and not _inside(dst, wiki)):
            raise Refused(f"{rel} in the vault is a link; not touching it")
        plan.append((status, rel, src, dst))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = state_dir() / "wiki" / "backups" / f"{stamp}-{req.get('id', 'x')}"
    written, deleted = [], []
    lock = _lock()
    try:
        for status, rel, src, dst in plan:
            if dst.exists():
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup / rel)
            if status == "D":
                if dst.exists():
                    dst.unlink()
                    deleted.append(rel)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f".{dst.name}.proxai-wiki.tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            written.append(rel)
    finally:
        lock.close()
    return {"ok": True, "written": written, "deleted": deleted,
            "backup": str(backup) if backup.exists() else None}


def main() -> int:
    req_file = Path(sys.argv[1])
    results = state_dir() / "wiki" / "results"
    if not _inside(req_file, state_dir() / "wiki" / "requests"):
        print("request must be in the wiki requests folder", file=sys.stderr)
        return 2
    req = json.loads(req_file.read_text(encoding="utf-8"))
    try:
        result = apply(req)
    except Refused as exc:
        result = {"ok": False, "error": str(exc)}
    except OSError as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{req.get('id', 'x')}.json").write_text(json.dumps(result), encoding="utf-8")
    req_file.unlink(missing_ok=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
