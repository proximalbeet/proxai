"""
ProxAI wiki synthesizer: turns new and changed vault notes into 10-Wiki pages.

Decided with Nathan 2026-10-23 ("middle option"):
* Claude Code writes the pages (Sonnet; Opus if a Sonnet run fails), in the sandbox, in a git
  workspace (~/dev/proxai/wiki-work = a copy of 10-Wiki). Every run is a commit, so it can be undone.
* Notes from the "auto" folders (courses, knowledge, projects, roles, career) go into 10-Wiki right
  away. Notes from the "review" folders (brain dump, daily, focus logs) are written on a separate
  branch and wait in ONE digest until Nathan approves it with a yes. Folders in policy.json.
* A note with `wiki: no` in its frontmatter is skipped. 01-Personal and 07-Security are never read.
* Runs when Nathan asks ("update the wiki") and nightly (policy.json nightly_at).
* promote.py (a sandboxed systemd job that sees only 10-Wiki) copies the changes into the vault,
  with backups and the shared vault lock. Every promotion is logged in 10-Wiki/log.md (by Claude
  Code), ~/.local/state/proxai/wiki.log and the security log (WIKI).

Chat: "update the wiki", "wiki status", "approve / reject the wiki update", "undo the last wiki
update" (phone, DMs, CLI). Tool `proxai_wiki` does the same.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import hashlib
import importlib.util
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
VAULT = Path(os.getenv("PROXAI_VAULT", "~/Documents/ProxVault")).expanduser()
STATE_DIR = Path(os.getenv("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser()
WORK_HOST = Path(os.getenv("PROXAI_WIKI_WORK", "~/dev/proxai/wiki-work")).expanduser()
WORK = "/workspace/proxai/wiki-work"          # the same folder inside the sandbox
VAULT_SANDBOX = "/workspace/vault"
PROJECT = "wiki-work"
RUN_MINUTES, RUN_TURNS = 30, 60
MAX_NOTE_BYTES = 200_000
_ALLOWED_PLATFORMS = {"api_server", "cli"}   # plus Discord DMs

_lock = threading.Lock()
_job: dict | None = None
_asked: dict[str, float] = {}     # session -> when we showed the digest (a plain yes then approves)


# ---- small helpers ----------------------------------------------------------------------------

def load_policy() -> dict:
    p = json.loads((HERE / "policy.json").read_text(encoding="utf-8"))
    never = set(p.get("never", [])) | {"01-Personal", "07-Security", "10-Wiki"}
    return {"auto": [f for f in p.get("auto", []) if f not in never],
            "review": [f for f in p.get("review", []) if f not in never],
            "never": sorted(never), "max": int(p.get("max_notes_per_run", 20)),
            "nightly_at": str(p.get("nightly_at", "03:30")), "model": p.get("model", "sonnet"),
            "fallback_model": p.get("fallback_model", "opus")}


def _state_file() -> Path:
    return STATE_DIR / "wiki" / "state.json"


def load_state() -> dict:
    try:
        s = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("sources", {})
    s.setdefault("promotions", [])
    s.setdefault("review", None)
    return s


def save_state(s: dict) -> None:
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def _log(rec: dict) -> None:
    try:
        (STATE_DIR).mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "wiki.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    except OSError:
        pass


_security_log = None


def _audit(text: str) -> None:
    global _security_log
    try:
        if _security_log is None:
            spec = importlib.util.spec_from_file_location("proxai_wiki_guard",
                                                          HERE.parent / "discord-routing" / "guard.py")
            guard = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(guard)
            _security_log = guard.SecurityLog(VAULT)
        _security_log.write("WIKI", "wiki", text)
    except Exception as exc:
        logger.warning("[wiki] security log failed: %s", exc)


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        return os.getenv(name, "")


def _where() -> tuple[str, bool]:
    if _session("HERMES_CRON_SESSION"):
        return "cron", False
    platform = _session("HERMES_SESSION_PLATFORM") or "cli"
    if platform == "discord":
        return ("dm", True) if _session("HERMES_SESSION_CHAT_TYPE") == "dm" else \
            ("#" + (_session("HERMES_SESSION_CHAT_NAME") or "channel"), False)
    return platform, platform in _ALLOWED_PLATFORMS


# ---- which notes are new ----------------------------------------------------------------------

_NO_WIKI = re.compile(r"^wiki\s*:\s*(no|false|off|skip)\s*$", re.I | re.M)


def _skipped(text: str) -> bool:
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    return bool(_NO_WIKI.search(text[:end if end > 0 else 2000]))


def scan(policy: dict, state: dict) -> dict:
    """{"auto": [(rel, hash)], "review": [...], "waiting": {"auto": n, "review": n}} for new/changed
    notes, newest first, at most policy max per batch. Rejected notes come back only if edited."""
    out = {}
    waiting = {}
    for kind in ("auto", "review"):
        found, pending = [], []
        for top in policy[kind]:
            root = VAULT / top
            if not root.is_dir() or root.is_symlink():
                continue
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in files:
                    if not name.endswith(".md") or name.startswith("."):
                        continue
                    p = Path(dirpath) / name
                    if p.is_symlink():
                        continue
                    try:
                        data = p.read_bytes()
                    except OSError:
                        continue
                    if len(data) > MAX_NOTE_BYTES or _skipped(data[:4000].decode("utf-8", "replace")):
                        continue
                    rel = p.relative_to(VAULT).as_posix()
                    h = hashlib.sha1(data).hexdigest()[:16]
                    known = state["sources"].get(rel) or {}
                    if known.get("hash") == h and known.get("status") in ("done", "rejected", "pending"):
                        if known.get("status") == "pending":
                            pending.append((p.stat().st_mtime, rel, h))
                        continue
                    found.append((p.stat().st_mtime, rel, h))
        found.sort(reverse=True)
        waiting[kind] = len(found)
        # Notes already waiting for Nathan's OK are redone only when new ones join the review.
        batch = found + sorted(pending, reverse=True) if found else []
        out[kind] = [(rel, h) for _, rel, h in batch[:policy["max"]]]
    out["waiting"] = waiting
    return out


# ---- the sandbox (git workspace + Claude Code) ------------------------------------------------

def _shell(command: str, task_id=None, timeout: int = 120) -> dict:
    from tools.terminal_tool import terminal_tool
    raw = terminal_tool(command=command, timeout=timeout, task_id=task_id)
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except ValueError:
        return {"output": str(raw)}


class Failed(Exception):
    pass


def sh(command: str, task_id=None, timeout: int = 120) -> str:
    r = _shell(f"cd {WORK} && {command}", task_id=task_id, timeout=timeout)
    if r.get("exit_code") not in (0, None) or r.get("error"):
        raise Failed(f"{command.split('&&')[0][:60]}: {(r.get('output') or r.get('error') or '')[-300:]}")
    return (r.get("output") or "").strip()


_SYNC_PY = r'''
import os, shutil, sys
src, dst = sys.argv[1], sys.argv[2]
seen = set()
for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if not d.startswith(".")]
    for f in files:
        s = os.path.join(root, f)
        if f.startswith(".") or os.path.islink(s):
            continue
        rel = os.path.relpath(s, src); seen.add(rel); d = os.path.join(dst, rel)
        if not os.path.exists(d) or open(s, "rb").read() != open(d, "rb").read():
            os.makedirs(os.path.dirname(d), exist_ok=True); shutil.copyfile(s, d)
for root, dirs, files in os.walk(dst):
    dirs[:] = [d for d in dirs if not d.startswith(".")]
    for f in files:
        p = os.path.join(root, f)
        if not f.startswith(".") and os.path.relpath(p, dst) not in seen:
            os.remove(p)
'''


def prepare(task_id=None) -> str:
    """Workspace = git repo on main, in step with the vault's 10-Wiki; returns HEAD."""
    _shell(f"mkdir -p {WORK}", task_id=task_id)
    sh("{ [ -d .git ] || { git init -q -b main && git config user.name ProxAI && "
       "git config user.email proxai@localhost && printf '.proxai/\\n' > .gitignore; }; }", task_id)
    sh("git checkout -q main 2>/dev/null || true", task_id)
    # index/hot/log only grow; when a waiting review and a newer update both add lines, keep both.
    sh("[ -f .gitattributes ] || { printf 'index.md merge=union\\nhot.md merge=union\\nlog.md merge=union\\n' "
       "> .gitattributes && git add .gitattributes && git commit -qm 'merge rules'; }", task_id)
    sh(f"python3 - {VAULT_SANDBOX}/10-Wiki {WORK} <<'PYEOF'\n{_SYNC_PY}\nPYEOF", task_id, timeout=300)
    sh("git add -A && { git diff --cached --quiet || git commit -qm 'sync from the vault'; }", task_id)
    # The vault already has whatever the sync brought in.
    sh("git tag -f promoted HEAD >/dev/null", task_id)
    return sh("git rev-parse HEAD", task_id)


def commit(message: str, task_id=None) -> str | None:
    """Commit everything Claude Code changed; None when it changed nothing."""
    sh("rm -rf .proxai/tmp; git add -A", task_id)
    if not sh("git status --porcelain", task_id):
        return None
    sh(f"git commit -qm {shlex.quote(message)}", task_id)
    return sh("git rev-parse HEAD", task_id)


def changes(a: str, b: str, task_id=None) -> list[list[str]]:
    out = sh(f"git diff --no-renames --name-status {shlex.quote(a)} {shlex.quote(b)}", task_id)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].endswith(".md") and not any(
                x.startswith(".") for x in parts[1].split("/")):
            rows.append([parts[0][:1], parts[1]])
    return rows


def _cc():
    spec = importlib.util.spec_from_file_location("proxai_wiki_claude_code",
                                                  HERE.parent / "claude-code-delegation" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GUIDE = """You maintain Nathan's Obsidian wiki: the folder you're in is a copy of the vault's 10-Wiki
(the wiki is ProxAI's main long-term memory). Turn the source notes below into wiki pages.

Before writing, read _meta/taxonomy.md, index.md and hot.md, and look at two existing pages in the
folder you're writing to so yours match them.

Pages:
- Put each page in the folder for its type: concepts/, entities/, skills/, references/, synthesis/,
  projects/. File names: lowercase-with-dashes.md. Update an existing page when it covers the topic
  instead of making a near-duplicate.
- Frontmatter like the existing pages: title, category, tags (max 5, from _meta/taxonomy.md; add a
  new domain tag to the taxonomy if really needed), sources (vault-relative paths of the notes you
  used, e.g. 03-Courses/x.md), created, updated (today), summary (one line), plus relationships
  when they're clear.
- Body: what someone needs to understand and use the topic, in your own words, with [[links]] to
  related pages (folder/page form like [[concepts/c-pointers]]). Facts come from the notes; don't
  invent. Keep each page focused (usually under 400 lines).
- Personal details (health, money, relationships, addresses, other people's private info) don't go
  into the wiki; summarise the useful knowledge only.
- Never delete or rename existing pages. Don't touch anything outside this folder. Don't run git.

After the pages:
- index.md: add or update one line per page, in its section, same style as the others.
- hot.md: add a dated line at the top of Recent Activity; refresh Active Threads if needed (~500 words).
- log.md: one line per source note, e.g.
  - [YYYY-MM-DD] INGEST source="03-Courses/x.md" pages_created=N pages_updated=M by=proxai-wiki
- Write .proxai/digest.md: up to 15 short bullets for Nathan, one per page created or updated, with
  why (e.g. "concepts/hash-tables (new): from Data Structures week 6").

Today is {today}. Source notes ({count}), readable at /workspace/vault/<path>:
{notes}
"""


def run_claude(notes: list[tuple[str, str]], policy: dict, task_id=None) -> dict:
    task = GUIDE.format(today=dt.date.today().isoformat(), count=len(notes),
                        notes="\n".join(f"- {rel}" for rel, _ in notes))
    job = {"task": task, "project": PROJECT, "max_turns": RUN_TURNS, "timeout_minutes": RUN_MINUTES}
    result: dict = {}
    for model in (policy["model"], policy["fallback_model"]):
        args = {**job, "model": model} if model else dict(job)
        result = json.loads(_cc()._handle_claude_code(args, task_id=task_id, session_id="proxai-wiki"))
        if "unknown argument: --model" in str(result.get("error") or ""):
            result = json.loads(_cc()._handle_claude_code(job, task_id=task_id, session_id="proxai-wiki"))
        result["model"] = model
        if result.get("ok"):
            break
    return result


# ---- writing into the vault -------------------------------------------------------------------

def _run_promote(req: dict) -> dict:
    """promote.py as a sandboxed systemd user job (sees only 10-Wiki, the workspace, its state)."""
    work = STATE_DIR / "wiki"
    (work / "requests").mkdir(parents=True, exist_ok=True)
    req_file = work / "requests" / f"{req['id']}.json"
    req_file.write_text(json.dumps(req), encoding="utf-8")
    hidden = " ".join(f"-{VAULT / d}" for d in ("01-Personal", "07-Security", ".git", ".obsidian"))
    props = ["ProtectHome=tmpfs", "ProtectSystem=strict", "PrivateTmp=yes", "NoNewPrivileges=yes",
             "PrivateNetwork=yes", f"BindPaths={VAULT / '10-Wiki'} {STATE_DIR}",
             f"BindReadOnlyPaths={WORK_HOST} {HERE}", f"InaccessiblePaths={hidden}"]
    argv = ["systemd-run", "--user", "--wait", "--collect", "--quiet", f"--unit=proxai-wiki-{req['id']}",
            *[x for p in props for x in ("-p", p)],
            "-E", f"PROXAI_VAULT={VAULT}", "-E", f"PROXAI_STATE_DIR={STATE_DIR}", "-E", f"PROXAI_WIKI_WORK={WORK_HOST}",
            shutil.which("python3") or "/usr/bin/python3", str(HERE / "promote.py"), str(req_file)]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=180)
        return json.loads((work / "results" / f"{req['id']}.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"the promote job failed: {exc}"}


def promote(kind: str, notes: list, state: dict, task_id=None) -> dict:
    """Copy promoted..HEAD into the vault; on success move the tag and remember it for undo."""
    before = sh("git rev-parse promoted", task_id)
    after = sh("git rev-parse HEAD", task_id)
    rows = changes(before, after, task_id)
    if not rows:
        return {"ok": True, "written": [], "deleted": []}
    result = _run_promote({"id": uuid.uuid4().hex[:12], "changes": rows})
    if result.get("ok"):
        sh("git tag -f promoted HEAD >/dev/null", task_id)
        state["promotions"].append({"kind": kind, "before": before, "after": after, "notes": [n for n, _ in notes],
                                    "files": len(rows), "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
        state["promotions"] = state["promotions"][-50:]
        _audit(f"{kind}: {len(result.get('written', []))} pages written, {len(result.get('deleted', []))} removed "
               f"from {len(notes)} notes")
    return result


# ---- the jobs ---------------------------------------------------------------------------------

def _mark(state: dict, notes, status: str) -> None:
    for rel, h in notes:
        state["sources"][rel] = {"hash": h, "status": status, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}


def run_update(trigger: str, task_id=None) -> dict:
    policy, state = load_policy(), load_state()
    found = scan(policy, state)
    summary = {"trigger": trigger, "auto": len(found["auto"]), "review": len(found["review"]),
               "waiting": found["waiting"]}
    if not found["auto"] and not found["review"]:
        state["last_run"] = {**summary, "ok": True, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "note": "nothing new"}
        save_state(state)
        return state["last_run"]
    prepare(task_id)
    if found["auto"]:
        cc = run_claude(found["auto"], policy, task_id)
        summary["auto_model"] = cc.get("model")
        sha = commit(f"proxai-wiki: {len(found['auto'])} notes ({trigger})", task_id) if cc.get("ok") else None
        if not cc.get("ok"):
            sh("git reset -q --hard HEAD && git clean -qfd -e .proxai", task_id)
            summary["auto_error"] = cc.get("error") or "Claude Code failed"
        elif sha:
            r = promote("auto", found["auto"], state, task_id)
            summary["auto_written"] = len(r.get("written", []))
            if r.get("ok"):
                _mark(state, found["auto"], "done")
                notify(f"📚 Wiki updated: {summary['auto_written']} pages",
                       f"From {len(found['auto'])} new or changed notes. Say \"undo the last wiki update\" to undo.")
            else:
                summary["auto_error"] = r.get("error")
        else:
            _mark(state, found["auto"], "done")      # nothing worth a page
    if found["review"]:
        sh("git checkout -q main && git branch -D review >/dev/null 2>&1; git checkout -q -b review main "
           "&& rm -f .proxai/digest.md", task_id)
        cc = run_claude(found["review"], policy, task_id)
        sha = commit(f"proxai-wiki review: {len(found['review'])} notes ({trigger})", task_id) if cc.get("ok") else None
        digest = sh("cat .proxai/digest.md 2>/dev/null | head -20 || true", task_id)
        if not cc.get("ok"):
            sh("git reset -q --hard HEAD && git clean -qfd -e .proxai", task_id)
        sh("git checkout -q main", task_id)
        if sha:
            state["review"] = {"notes": found["review"], "head": sha, "digest": digest[:2000],
                               "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "announced": False}
            _mark(state, found["review"], "pending")
            summary["review_pages"] = len(changes("main", "review", task_id))
            notify("📚 Wiki update waiting for your OK",
                   f"{len(found['review'])} brain-dump/daily/focus notes. Say yes in chat, or \"approve\" / "
                   f"\"reject the wiki update\".\n{digest[:600]}")
        elif cc.get("ok"):
            _mark(state, found["review"], "done")
            sh("git branch -D review >/dev/null 2>&1 || true", task_id)
        else:
            summary["review_error"] = cc.get("error") or "Claude Code failed"
    state["last_run"] = {**summary, "ok": not any(k.endswith("_error") for k in summary),
                         "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_state(state)
    return state["last_run"]


def approve(task_id=None) -> dict:
    state = load_state()
    rev = state.get("review")
    if not rev:
        return {"ok": False, "error": "No wiki update is waiting."}
    prepare(task_id)
    notes = [tuple(n) for n in rev["notes"]]
    try:
        sh("git rebase -q main review", task_id)
        sh("git checkout -q main && git merge -q --ff-only review && git branch -D review >/dev/null", task_id)
    except Failed:
        # A page changed on both sides: write those notes again on top of today's wiki.
        sh("git rebase --abort 2>/dev/null; git checkout -q -f main; git branch -D review >/dev/null 2>&1 || true",
           task_id)
        cc = run_claude(notes, load_policy(), task_id)
        if not cc.get("ok") or not commit(f"proxai-wiki review (redone on approval): {len(notes)} notes", task_id):
            sh("git reset -q --hard HEAD && git clean -qfd -e .proxai", task_id)
            return {"ok": False, "error": cc.get("error") or "Redoing the waiting update made no pages."}
    r = promote("review", notes, state, task_id)
    if r.get("ok"):
        _mark(state, notes, "done")
        state["review"] = None
    save_state(state)
    return {"ok": bool(r.get("ok")), "written": len(r.get("written", [])), "error": r.get("error")}


def reject() -> dict:
    state = load_state()
    rev = state.get("review")
    if not rev:
        return {"ok": False, "error": "No wiki update is waiting."}
    try:
        sh("git checkout -q main && git branch -D review >/dev/null 2>&1 || true")
    except Failed:
        pass
    _mark(state, [tuple(n) for n in rev["notes"]], "rejected")
    state["review"] = None
    save_state(state)
    _audit(f"review rejected ({len(rev['notes'])} notes)")
    return {"ok": True, "notes": len(rev["notes"])}


def undo(task_id=None) -> dict:
    state = load_state()
    if not state["promotions"]:
        return {"ok": False, "error": "There's no wiki update to undo."}
    last = state["promotions"][-1]
    prepare(task_id)
    try:
        sh(f"git revert --no-edit {shlex.quote(last['before'])}..{shlex.quote(last['after'])}", task_id)
    except Failed:
        sh("git revert --abort 2>/dev/null || true", task_id)
        return {"ok": False, "error": "Later wiki changes touch the same pages, so it can't be undone cleanly."}
    count = len(state["promotions"])
    r = promote("undo", [], state, task_id)
    if r.get("ok"):
        # Forget the undone update and the undo itself, so the next undo goes one further back.
        state["promotions"] = state["promotions"][:count - 1]
        for rel in last.get("notes", []):
            if rel in state["sources"]:
                state["sources"][rel]["status"] = "rejected"     # don't bring it back unless edited
        save_state(state)
    return {"ok": bool(r.get("ok")), "undone": last["kind"], "files": len(r.get("written", [])) +
            len(r.get("deleted", [])), "error": r.get("error")}


def _run_in_background(fn, label: str, *args) -> str | None:
    global _job
    with _lock:
        if _job is not None:
            return f"The wiki is busy ({_job['label']}, {int(time.time() - _job['started'])}s). Try again when it's done."
        _job = {"label": label, "started": time.time()}

    def body():
        global _job
        try:
            result = fn(*args)
        except Exception as exc:     # never kill the gateway thread
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _log({"job": label, **{k: v for k, v in (result or {}).items() if k != "digest"}})
        with _lock:
            _job = None

    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(body,), name=f"proxai-wiki-{label}", daemon=True).start()
    return None


def status_text() -> str:
    state = load_state()
    lines = []
    if _job:
        lines.append(f"Running now: {_job['label']} ({int(time.time() - _job['started'])}s).")
    lr = state.get("last_run")
    if lr:
        bits = [f"last run {lr['time'][5:16].replace('T', ' ')} ({lr['trigger']})"]
        if lr.get("note"):
            bits.append(lr["note"])
        if lr.get("auto"):
            bits.append(f"{lr['auto']} notes went in" + (f", {lr['auto_written']} pages" if lr.get("auto_written") else ""))
        for k in ("auto_error", "review_error"):
            if lr.get(k):
                bits.append(f"problem: {str(lr[k])[:160]}")
        lines.append("; ".join(bits) + ".")
    rev = state.get("review")
    if rev:
        lines.append(f"Waiting for Nathan's OK: {len(rev['notes'])} notes (brain dump/daily/focus), "
                     f"from {rev['created'][5:16].replace('T', ' ')}:\n{rev.get('digest') or '(no digest)'}")
    if state["promotions"]:
        p = state["promotions"][-1]
        lines.append(f"Last change to 10-Wiki: {p['kind']}, {p['files']} files, {p['time'][5:16].replace('T', ' ')} "
                     "(can be undone).")
    return "\n".join(lines) or "The wiki synthesizer hasn't run yet."


# ---- chat ------------------------------------------------------------------------------------

_RUN = re.compile(r"\b(update|refresh|run|sync|rebuild|process)\b.{0,25}\bwiki\b|\bwiki\b.{0,10}\b(update|refresh|ingest)\b"
                  r"(?!.{0,20}\b(waiting|pending)\b)", re.I | re.S)
_STATUS = re.compile(r"\bwiki\b.{0,30}\b(status|waiting|pending|digest|doing)\b|"
                     r"\b(what'?s|what is) (waiting|pending)\b.{0,20}\bwiki\b", re.I | re.S)
_APPROVE = re.compile(r"\b(approve|accept|apply|merge)\b.{0,30}\bwiki\b", re.I | re.S)
_REJECT = re.compile(r"\b(reject|skip|drop|discard|don'?t add)\b.{0,30}\bwiki\b", re.I | re.S)
_UNDO = re.compile(r"\b(undo|revert|roll ?back)\b.{0,30}\bwiki\b", re.I | re.S)
_YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|approve|approved|do it|go ahead)\b[\s,.!]*$", re.I)
_QUESTION = re.compile(r"^\s*(how|why|what does|explain)\b", re.I)


def _plain(user_message) -> str:
    if isinstance(user_message, list):
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    return re.sub(r"\[[^\]]*\]", " ", str(user_message or "")).strip()


def _started(err: str | None, what: str) -> dict:
    if err:
        return {"context": f"[wiki] Couldn't start: {err} Tell Nathan in one sentence."}
    return {"context": f"[wiki] Done for you: ALREADY started {what} in the background (Claude Code on the PC). "
                       "Don't call any tool. Tell Nathan in one sentence; he can ask \"wiki status\" later."}


_schedule_module = None


def _schedule():
    """The proxai-schedule plugin: time phrases, approved phone jobs, the phone notice feed."""
    global _schedule_module
    if _schedule_module is None:
        spec = importlib.util.spec_from_file_location("proxai_wiki_schedule", HERE.parent / "proxai-schedule" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _schedule_module = mod
    return _schedule_module


def notify(title: str, text: str = "") -> None:
    try:
        _schedule().notices.post(title, text, kind="wiki")
    except Exception as exc:
        logger.warning("[wiki] notice failed: %s", exc)


def _request_file() -> Path:
    return STATE_DIR / "wiki" / "run-request"


def _on_pre_llm_call(session_id=None, user_message=None, parent_session_id=None, task_id=None, **_kwargs):
    if parent_session_id:
        return None
    text = _plain(user_message)
    if str(task_id or "").startswith("cron:"):
        # An approved phone job ("update the wiki every night at 3"): the gateway runs it (this process
        # is a short-lived cron worker).
        if _RUN.search(text) and _schedule().is_approved_cron(task_id):
            _request_file().parent.mkdir(parents=True, exist_ok=True)
            _request_file().write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
            return {"context": "[wiki] Approved scheduled job: the wiki update starts on the PC within a minute. "
                               "Reply in one sentence."}
        return None
    _where_now, allowed = _where()
    if not allowed:
        return None
    try:
        if _schedule().parse_request(text, dt.datetime.now()):
            return None     # "... every night at 3": proxai-schedule asks to schedule it
    except Exception as exc:
        logger.warning("[wiki] schedule check failed: %s", exc)
    sid = session_id or ""
    asked = _asked.pop(sid, 0)
    if asked and time.time() - asked < 900 and _YES.match(text) and load_state().get("review"):
        return _started(_run_in_background(approve, "approve"), "adding the waiting wiki update")
    if "wiki" not in text.lower():
        state = load_state()
        rev = state.get("review")
        if rev and not rev.get("announced"):
            rev["announced"] = True
            save_state(state)
            _asked[sid] = time.time()
            return {"context": "[wiki] At the END of your reply, add one or two short lines: a wiki update is "
                               f"waiting for his OK ({len(rev['notes'])} notes from brain dump/daily/focus). Pages:\n"
                               f"{(rev.get('digest') or '')[:600]}\nHe can say yes (or \"approve the wiki update\" / "
                               "\"reject the wiki update\")."}
        return None
    if _QUESTION.search(text) and not _STATUS.search(text):
        return None
    if _UNDO.search(text):
        return _started(_run_in_background(undo, "undo"), "undoing the last wiki update")
    if _REJECT.search(text):
        r = reject()
        return {"context": "[wiki] " + (f"Done: the waiting update ({r['notes']} notes) was dropped; those notes "
                                        "won't come back unless he edits them." if r["ok"] else r["error"]) +
                           " Tell Nathan in one sentence."}
    if _APPROVE.search(text):
        if not load_state().get("review"):
            return {"context": "[wiki] No wiki update is waiting. Tell Nathan in one sentence."}
        return _started(_run_in_background(approve, "approve"), "adding the waiting wiki update")
    if _STATUS.search(text):
        _asked[sid] = time.time()
        return {"context": "[wiki] Wiki synthesizer status (tell Nathan briefly; if an update is waiting he can say "
                           "yes to add it):\n" + status_text()}
    if _RUN.search(text):
        return _started(_run_in_background(run_update, "update", "request"), "a wiki update from his new notes")
    return None


# ---- tool, nightly ---------------------------------------------------------------------------

SCHEMA = {
    "name": "proxai_wiki",
    "description": ("Nathan's wiki synthesizer (10-Wiki). run: turn new notes into wiki pages now (background). "
                    "status: last run, anything waiting. approve / reject: the waiting brain-dump/daily/focus "
                    "update (only after Nathan said so). undo: revert the last change to 10-Wiki."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["run", "status", "approve", "reject", "undo"]}}, "required": ["action"]},
}


def _handle(args: dict | None = None, **_kw) -> str:
    action = str((args or {}).get("action") or "")
    _where_now, allowed = _where()
    if action == "status":
        return json.dumps({"ok": True, "status": status_text()})
    if not allowed:
        return json.dumps({"ok": False, "error": "Only from Nathan's DMs, the phone app or the CLI."})
    jobs = {"run": (run_update, "update", "request"), "approve": (approve, "approve"), "undo": (undo, "undo")}
    if action in jobs:
        fn, label, *rest = jobs[action]
        err = _run_in_background(fn, label, *rest)
        return json.dumps({"ok": err is None, "started": err is None, "error": err})
    if action == "reject":
        return json.dumps(reject())
    return json.dumps({"ok": False, "error": f"unknown action '{action}'"})


def _nightly_due(now: dt.datetime, policy: dict, state: dict) -> bool:
    hh, mm = (int(x) for x in policy["nightly_at"].split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start <= now < start + dt.timedelta(hours=3) and state.get("last_nightly") != now.date().isoformat()


def _nightly_loop() -> None:
    while True:
        time.sleep(60)
        try:
            state = load_state()
            now = dt.datetime.now()
            if _nightly_due(now, load_policy(), state) and _job is None:
                state["last_nightly"] = now.date().isoformat()
                save_state(state)
                _run_in_background(run_update, "nightly", "nightly")
            elif _request_file().exists() and _job is None:
                _request_file().unlink(missing_ok=True)
                _run_in_background(run_update, "scheduled", "scheduled")
        except Exception as exc:
            logger.warning("[wiki] nightly check failed: %s", exc)


def register(ctx) -> None:
    ctx.register_tool(name="proxai_wiki", toolset="proxai_wiki", schema=SCHEMA, handler=_handle, emoji="📚",
                      description="Turn new vault notes into wiki pages; approve, reject or undo updates")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    import sys
    in_gateway = "gateway" in sys.argv and "run" in sys.argv   # not in short-lived cron workers
    if in_gateway and os.getenv("PROXAI_WIKI_NIGHTLY", "on") != "off":
        threading.Thread(target=_nightly_loop, name="proxai-wiki-nightly", daemon=True).start()
