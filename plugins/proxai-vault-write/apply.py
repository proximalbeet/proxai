#!/usr/bin/env python3
"""Writes one vault note for the proxai-vault-write plugin.

  apply.py <requests/ID.json>

The plugin starts this as its own short systemd user job (``systemd-run --wait``), because the
gateway's sandbox keeps most of the vault read-only. The job has a sandbox of its own: it sees
only the vault and the ProxAI state folder, with 01-Personal, 07-Security, .git and .obsidian
hidden, and no network.

It checks the request again (vault_rules.check + "ask" folders need ``approved_by``), refuses
symlinks, takes the shared vault lock (same as the refiler and vault sync), backs up any file it
changes to ~/.local/state/proxai/vault-write/backups/, writes atomically, and leaves
results/ID.json plus a line in log.jsonl. Exit 0 = written, 1 = refused/failed.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_rules import Refused, check, load_policy, state_dir, vault_dir  # noqa: E402

LOCK_WAIT_SECS = 20


def _work() -> Path:
    return state_dir() / "vault-write"


def _no_symlinks(vault: Path, rel: str) -> Path:
    """The target path, refusing any symlink on the way (a link could point out of the vault)."""
    real_vault = vault.resolve()
    cur = vault
    for part in rel.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise Refused(f"{rel} goes through a symlink; not writing.")
    target = vault / rel
    if not str(target.resolve()).startswith(str(real_vault) + os.sep):
        raise Refused(f"{rel} resolves outside the vault.")
    return target


def _locked(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = lock_path.open("w")
    deadline = time.monotonic() + LOCK_WAIT_SECS
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() > deadline:
                f.close()
                raise Refused("The vault is busy (sync or refiler running); try again in a minute.")
            time.sleep(0.5)


def apply(req: dict) -> dict:
    policy = load_policy()
    rel, rule = check(req.get("path", ""), req.get("content", ""), req.get("how", "create"), policy)
    if rule == "ask" and not req.get("approved_by"):
        raise Refused(f"{rel} needs Nathan's approval first.")
    vault = vault_dir()
    if not vault.is_dir():
        raise Refused(f"vault not found at {vault}")
    target = _no_symlinks(vault, rel)
    how, content = req["how"], str(req.get("content", ""))

    lock = _locked(state_dir() / "vault.lock")
    try:
        exists = target.exists()
        if how == "create" and exists:
            raise Refused(f"{rel} already exists; use replace or append.")
        if how == "replace" and not exists:
            raise Refused(f"{rel} doesn't exist; use create.")
        if exists and not target.is_file():
            raise Refused(f"{rel} is a folder, not a note.")

        backup = None
        if exists:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = _work() / "backups" / f"{stamp}-{req['id']}" / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(target.read_bytes())
            backup = str(backup)

        if how == "append" and exists:
            old = target.read_text(encoding="utf-8")
            sep = "" if not old or old.endswith("\n") else "\n"
            new = old + sep + content
        else:
            new = content
        if not new.endswith("\n"):
            new += "\n"

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.vault-write-tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        lock.close()
    return {"ok": True, "id": req["id"], "path": rel, "how": how, "rule": rule,
            "created": not exists, "bytes": len(new.encode("utf-8")), "backup": backup}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    req_file = Path(argv[1])
    work = _work()
    try:
        if req_file.resolve().parent != (work / "requests").resolve():
            raise Refused("request file must be in the vault-write requests folder")
        req = json.loads(req_file.read_text(encoding="utf-8"))
        result = apply(req)
    except Refused as exc:
        result = {"ok": False, "refused": True, "error": str(exc)}
    except Exception as exc:  # report, never crash silently
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    rid = req_file.stem
    result.setdefault("id", rid)
    result["time"] = dt.datetime.now().isoformat(timespec="seconds")
    (work / "results").mkdir(parents=True, exist_ok=True)
    (work / "results" / f"{rid}.json").write_text(json.dumps(result), encoding="utf-8")
    with (work / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in result.items() if k != "content"}) + "\n")
    try:
        req_file.unlink()   # the request held the note text; the result doesn't need it
    except OSError:
        pass
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
