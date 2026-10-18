#!/usr/bin/env python3
"""ProxAI vault auto-sync (Phase 8): keep ~/Documents/ProxVault in step with GitHub.

Runs on the host from a systemd user timer. Each run:
  1. takes ~/.local/state/proxai/vault.lock (shared with the refiler, so they never overlap)
  2. skips if the repo is mid-rebase/merge, or a changed file was written in the last few seconds
  3. commits local changes
  4. fetches and rebases onto the remote branch (--autostash); never merges, never force-pushes
  5. pushes

On a conflict the rebase is aborted, so the working files are left exactly as they were.
The run is recorded in vault-sync.json (latest state, read by the Hermes vault-sync plugin)
and vault-sync.log (one JSON line per run).

Usage:
  vault_sync.py              run once
  vault_sync.py --dry-run    show what would be committed; no commit, fetch or push
  vault_sync.py --status     print the last recorded state
  vault_sync.py prepare [--apply]
                             one-time switch-over: install .gitignore, stop tracking per-device
                             Obsidian files, then sync (see README.md). Plan only without --apply.

Python stdlib only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
VAULT = Path(os.environ.get("PROXAI_VAULT", HOME / "Documents/ProxVault"))
STATE_DIR = Path(os.environ.get("PROXAI_STATE", HOME / ".local/state/proxai"))
REMOTE = os.environ.get("PROXAI_VAULT_REMOTE", "git@github.com:proximalbeet/ProxVault.git")
BRANCH = os.environ.get("PROXAI_VAULT_BRANCH", "main")
SSH_KEY = Path(os.environ.get("PROXAI_VAULT_SSH_KEY", HOME / ".ssh/proxvault_deploy"))
KNOWN_HOSTS = Path(os.environ.get("PROXAI_VAULT_KNOWN_HOSTS", HERE / "github_known_hosts"))
GIT_NAME = os.environ.get("PROXAI_VAULT_GIT_NAME", "prox (desktop vault-sync)")
GIT_EMAIL = os.environ.get("PROXAI_VAULT_GIT_EMAIL", "vault-sync@stormtrooper")
QUIET_SECS = float(os.environ.get("PROXAI_VAULT_QUIET_SECS", "15"))
LOCK_WAIT_SECS = float(os.environ.get("PROXAI_VAULT_LOCK_WAIT", "120"))
NET_TIMEOUT = 90
MAX_FILE_BYTES = 50 * 1024 * 1024  # GitHub warns at 50 MB and refuses 100 MB

TRACKING_REF = f"refs/remotes/origin/{BRANCH}"
# Folder names never shown in the state file / Discord alerts (the host log keeps full paths).
PRIVATE_DIRS = ("01-Personal/", "07-Security/")
# Append-only logs written on every Discord message: committing mid-write is fine (the rest
# lands next run), so they never make a run wait.
QUIET_EXEMPT = ("07-Security/ProxAI-Audit/",)
# Per-device Obsidian state: never tracked (also listed in vault.gitignore).
UNTRACK = (".obsidian/workspace.json", ".obsidian/workspace-mobile.json")


class SyncError(Exception):
    def __init__(self, result: str, message: str, **extra):
        super().__init__(message)
        self.result, self.message, self.extra = result, message, extra


# --------------------------------------------------------------------------- helpers

def mask(path: str) -> str:
    for d in PRIVATE_DIRS:
        if path.startswith(d):
            return d + "<hidden>"
    return path


def git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": GIT_NAME, "GIT_AUTHOR_EMAIL": GIT_EMAIL,
        "GIT_COMMITTER_NAME": GIT_NAME, "GIT_COMMITTER_EMAIL": GIT_EMAIL,
        "GIT_EDITOR": "true",  # rebase must never open an editor
        "LC_ALL": "C",
    })
    if REMOTE.startswith("git@") or REMOTE.startswith("ssh://"):
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes "
            f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={KNOWN_HOSTS} "
            "-o ConnectTimeout=20"
        )
    return env


def git(*args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    # credential.helper= : never fall back to ~/.git-credentials (plaintext token)
    cmd = ["git", "-C", str(VAULT), "-c", "credential.helper=", "-c", "core.quotepath=off",
           "-c", "commit.gpgsign=false", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=git_env(), timeout=timeout)
    if check and proc.returncode != 0:
        raise SyncError("error", f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc


def rev(ref: str) -> str | None:
    p = git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    return p.stdout.strip() or None


def git_dir() -> Path:
    return Path(git("rev-parse", "--absolute-git-dir").stdout.strip())


def changed_paths() -> list[str]:
    """Paths git would stage with `add -A` (tracked changes + untracked, ignores respected)."""
    out = git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    paths, items, i = [], out.split("\0"), 0
    while i < len(items):
        entry = items[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":  # rename/copy: the next item is the old path
                i += 1
        i += 1
    return paths


def preflight() -> None:
    if not (VAULT / ".git").exists():
        raise SyncError("blocked", f"{VAULT} is not a git repository")
    gd = git_dir()
    for marker, op in (("rebase-merge", "rebase"), ("rebase-apply", "rebase/am"),
                       ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                       ("REVERT_HEAD", "revert")):
        if (gd / marker).exists():
            raise SyncError("blocked", f"vault repo is in the middle of a {op}; finish or abort it by hand")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    if branch != BRANCH:
        raise SyncError("blocked", f"vault is on '{branch or 'detached HEAD'}', not '{BRANCH}'")
    if git("diff", "--name-only", "--diff-filter=U").stdout.strip():
        raise SyncError("blocked", "vault has unresolved conflict markers from an earlier merge")


def recently_written(paths: list[str]) -> list[str]:
    now, busy = time.time(), []
    for p in paths:
        if p.startswith(QUIET_EXEMPT):
            continue
        try:
            if now - (VAULT / p).stat().st_mtime < QUIET_SECS:
                busy.append(p)
        except FileNotFoundError:
            pass  # deleted file
    return busy


def oversized(paths: list[str]) -> list[str]:
    big = []
    for p in paths:
        try:
            f = VAULT / p
            if f.is_file() and f.stat().st_size > MAX_FILE_BYTES:
                big.append(p)
        except OSError:
            pass
    return big


# --------------------------------------------------------------------------- the sync

def commit_local(summary: dict) -> None:
    paths = changed_paths()
    if not paths:
        return
    busy = recently_written(paths)
    if busy:
        raise SyncError("skipped", "a note is being written right now; trying again next run",
                        busy=[mask(p) for p in busy[:5]])
    git("add", "-A")
    big = oversized(paths)
    for p in big:
        git("reset", "--quiet", "--", p)
    if big:
        summary["warnings"].append(f"not synced (over 50 MB): {', '.join(mask(p) for p in big)}")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    n = len(git("diff", "--cached", "--name-only").stdout.splitlines())
    git("commit", "--quiet", "--no-verify", "-m",
        f"vault-sync: desktop {time.strftime('%Y-%m-%d %H:%M')} ({n} file{'s' if n != 1 else ''})")
    summary["committed"] = n


def fetch() -> None:
    git("fetch", "--quiet", "--no-tags", REMOTE, f"+refs/heads/{BRANCH}:{TRACKING_REF}",
        timeout=NET_TIMEOUT)


def rebase(summary: dict) -> None:
    head, upstream = rev("HEAD"), rev(TRACKING_REF)
    if upstream is None or upstream == head:
        return
    if git("merge-base", "--is-ancestor", upstream, "HEAD", check=False).returncode == 0:
        return  # remote has nothing we lack
    behind = int(git("rev-list", "--count", f"HEAD..{TRACKING_REF}").stdout.strip() or 0)
    p = git("rebase", "--autostash", TRACKING_REF, check=False, timeout=120)
    if p.returncode == 0:
        summary["pulled"] = behind
        return
    conflicts = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    detail = (p.stderr or p.stdout).strip()
    git("rebase", "--abort", check=False)
    after = rev("HEAD")
    if after != head or any((git_dir() / m).exists() for m in ("rebase-merge", "rebase-apply")):
        raise SyncError("blocked", "rebase failed and could not be undone cleanly; check the vault by hand",
                        host_detail=detail[-600:])
    raise SyncError("conflict",
                    f"laptop and desktop both changed the same lines; nothing was changed, "
                    f"sync paused until it is fixed by hand ({behind} remote commit(s) waiting)",
                    conflict_files=sorted({mask(c) for c in conflicts}) or ["(see host log)"],
                    host_conflict_files=conflicts, host_detail=detail[-600:])


def push(summary: dict) -> None:
    head, upstream = rev("HEAD"), rev(TRACKING_REF)
    if head == upstream:
        return
    if upstream and git("merge-base", "--is-ancestor", upstream, "HEAD", check=False).returncode != 0:
        raise SyncError("error", "local branch is not ahead of the remote after rebase; not pushing")
    ahead = int(git("rev-list", "--count", f"{TRACKING_REF}..HEAD").stdout.strip()) if upstream else 0
    p = git("push", "--quiet", "--porcelain", REMOTE, f"HEAD:refs/heads/{BRANCH}", check=False,
            timeout=NET_TIMEOUT)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()
        if "rejected" in msg or "non-fast-forward" in msg or "fetch first" in msg:
            raise SyncError("retry", "the laptop pushed at the same moment; will pull and retry next run")
        raise SyncError("error", f"push failed: {msg[-400:]}")
    git("update-ref", TRACKING_REF, "HEAD")
    summary["pushed"] = ahead


def sync_once(dry_run: bool = False) -> dict:
    summary = {"committed": 0, "pulled": 0, "pushed": 0, "warnings": []}
    preflight()
    if dry_run:
        paths = changed_paths()
        summary["would_commit"] = [mask(p) for p in paths]
        summary["busy"] = [mask(p) for p in recently_written(paths)]
        summary["result"], summary["message"] = "dry-run", f"{len(paths)} changed file(s) would be committed"
        return summary
    commit_local(summary)
    fetch()
    rebase(summary)
    push(summary)
    summary["result"] = "ok"
    parts = [f"{summary[k]} {label}" for k, label in
             (("committed", "file(s) committed"), ("pulled", "commit(s) pulled"), ("pushed", "commit(s) pushed"))
             if summary[k]]
    summary["message"] = ", ".join(parts) or "already in sync"
    return summary


# --------------------------------------------------------------------------- state + lock

def write_state(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    host_only = {k: record.pop(k) for k in list(record) if k.startswith("host_")}
    with (STATE_DIR / "vault-sync.log").open("a") as log:
        log.write(json.dumps({**record, **host_only}) + "\n")
    prev = read_state()
    if record["result"] in ("conflict", "blocked", "error"):
        # Same problem on consecutive runs keeps the same key, so Hermes alerts once, not every 10 min.
        basis = json.dumps([record["result"], record.get("conflict_files"), record["message"]])
        record["alert_key"] = hashlib.sha1(basis.encode()).hexdigest()[:12]
        record["since"] = prev.get("since") if prev.get("alert_key") == record["alert_key"] else record["time"]
    if record["result"] in ("skipped", "locked"):
        record["last_ok"] = prev.get("last_ok")
    elif record["result"] == "ok":
        record["last_ok"] = record["time"]
    else:
        record["last_ok"] = prev.get("last_ok")
    tmp = STATE_DIR / "vault-sync.json.tmp"
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(STATE_DIR / "vault-sync.json")


def read_state() -> dict:
    try:
        return json.loads((STATE_DIR / "vault-sync.json").read_text())
    except (OSError, ValueError):
        return {}


def with_lock(fn):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "vault.lock").open("w") as lock:
        deadline = time.monotonic() + LOCK_WAIT_SECS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise SyncError("locked", "the refiler (or another sync) held the vault lock too long")
                time.sleep(1)
        return fn()


def run(dry_run: bool = False) -> int:
    started = time.monotonic()
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        record.update(with_lock(lambda: sync_once(dry_run)))
    except SyncError as e:
        record.update({"result": e.result, "message": e.message, **e.extra})
    except subprocess.TimeoutExpired as e:
        record.update({"result": "error", "message": f"git timed out ({' '.join(map(str, e.cmd[5:7]))})"})
    record["duration_s"] = round(time.monotonic() - started, 1)
    if dry_run:
        print(json.dumps(record, indent=2))
        return 0
    write_state(record)
    print(f"{record['result']}: {record['message']}")
    return 0 if record["result"] in ("ok", "skipped", "locked", "retry") else 1


# --------------------------------------------------------------------------- one-time switch-over

def prepare(apply: bool) -> int:
    """Install .gitignore and stop tracking per-device Obsidian files without a conflict.

    The laptop has also changed workspace.json, so untracking it before pulling would be a
    modify/delete conflict. Order: back up the desktop copy, reset it to the last commit,
    sync (commit the rest, pull, push), then untrack + .gitignore, sync again, restore the backup.
    """
    template = (HERE / "vault.gitignore").read_text()
    target = VAULT / ".gitignore"
    current = target.read_text() if target.exists() else ""
    tracked = [p for p in UNTRACK if git("ls-files", "--error-unmatch", p, check=False).returncode == 0]
    plan = [
        "Close Obsidian on this computer first (it rewrites workspace.json while open).",
        f"Back up then reset to the last commit: {', '.join(tracked) or '(nothing tracked)'}",
        "Sync once: commit desktop changes, pull the laptop's commits, push",
        f"{'Write' if not current else 'Merge missing lines into'} .gitignore from vault-sync/vault.gitignore",
        f"Stop tracking (files stay on disk): {', '.join(tracked) or '(none)'}",
        "Commit 'vault-sync: ignore per-device Obsidian state', sync again",
        "Restore the backed-up workspace files (now ignored)",
    ]
    if not apply:
        print("prepare (plan only; run with --apply to do it):")
        for i, step in enumerate(plan, 1):
            print(f"  {i}. {step}")
        return 0

    backup_dir = STATE_DIR / "vault-sync-backup" / time.strftime("%Y%m%d-%H%M%S")
    for p in tracked:
        if (VAULT / p).exists():
            (backup_dir / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(VAULT / p, backup_dir / p)
    print(f"backup: {backup_dir}")

    def steps() -> dict:
        preflight()
        for p in tracked:
            git("checkout", "HEAD", "--", p)
        # Not the timer's code path: no quiet-period skip during the one-time run.
        summary = {"committed": 0, "pulled": 0, "pushed": 0, "warnings": []}
        commit_local_force(summary)
        fetch(); rebase(summary); push(summary)
        missing = [ln for ln in template.splitlines() if ln.strip() and ln not in current.splitlines()]
        if missing:
            target.write_text(current + ("\n" if current and not current.endswith("\n") else "")
                              + "\n".join(missing) + "\n")
        if tracked:
            git("rm", "--cached", "--quiet", "--", *tracked)
        git("add", ".gitignore")
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            git("commit", "--quiet", "--no-verify", "-m", "vault-sync: ignore per-device Obsidian state")
        fetch(); rebase(summary); push(summary)
        summary["result"], summary["message"] = "ok", "prepared: .gitignore in place, per-device files untracked"
        return summary

    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        record.update(with_lock(steps))
    except SyncError as e:
        record.update({"result": e.result, "message": e.message, **e.extra})
    finally:
        for p in tracked:
            if (backup_dir / p).exists():
                shutil.copy2(backup_dir / p, VAULT / p)
    write_state(record)
    print(f"{record['result']}: {record['message']}")
    if record["result"] != "ok":
        print("Nothing was force-changed. Fix the reported problem, then run prepare --apply again.")
    return 0 if record["result"] == "ok" else 1


def commit_local_force(summary: dict) -> None:
    global QUIET_SECS
    saved, QUIET_SECS = QUIET_SECS, 0
    try:
        commit_local(summary)
    finally:
        QUIET_SECS = saved


def main(argv: list[str]) -> int:
    if "--status" in argv:
        print(json.dumps(read_state(), indent=2))
        return 0
    if argv and argv[0] == "prepare":
        return prepare("--apply" in argv)
    return run(dry_run="--dry-run" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
