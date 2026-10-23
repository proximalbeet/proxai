"""
ProxAI apps (checklist Phase 15): Nathan asks for an app on the phone; Claude Code builds it in
the sandbox as an APK that ProxMobile's Apps tab installs.

* ``proxai_app`` tool: create / update / rollback / status / list.
* Only from the phone app (api_server) and the local CLI: apps are a ProxMobile feature.
* create/update/rollback return at once; the work runs in the background (one build at a time,
  the sandbox has 2 CPUs) through the claude-code-delegation plugin's own runner, so the same
  limits, logging and sandbox apply. Claude Code works in /workspace/proxai/apps/<id>/
  (= ~/dev/proxai/apps/<id>/), follows /opt/proxai-shell/GUIDE.md, reports progress with
  ``proxai-app-build --status`` and finishes with ``proxai-app-build`` (signed APK, git tag).
* The phone reads progress and downloads APKs from the apps server (ProxMobile
  server/proxai_apps_server.py), which reads the same folder.
* Every job is logged to ~/.local/state/proxai/apps.log (JSONL).
* Hermes keeps plugin tools behind tool_search, and the local model doesn't use them reliably
  (10-22: it wrote a Pomodoro app into the chat, then with a hint said "I'll build this" and
  stopped). So for a clear NEW-app request from the phone/CLI, the ``pre_llm_call`` hook starts the
  build itself and tells the model it's already running; the model only has to confirm. Questions
  ("how would I build…?") and "quick … in the chat" are left alone. Changes/rollbacks to existing
  apps still go through the tool, with a hint.
"""
from __future__ import annotations

import contextvars
import importlib.util
import json
import logging
import os
import re
import shlex
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
APPS_HOST = Path(os.getenv("PROXAI_APPS_DIR", "~/dev/proxai/apps")).expanduser()
APPS_SANDBOX = "/workspace/proxai/apps"
LOG = Path(os.getenv("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser() / "apps.log"
ALLOWED_PLATFORMS = {"api_server", "cli"}
BUILD_MINUTES, BUILD_TURNS = 20, 40
MAX_REQUEST = 4000

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")

_lock = threading.Lock()
_job: dict | None = None          # the running job: {"app", "kind", "started", "name"}
_last: dict[str, dict] = {}       # app id -> last job result

SCHEMA = {
    "name": "proxai_app",
    "description": (
        "Build Nathan a real Android app (APK) for his phone, made by Claude Code in the sandbox and "
        "installed from ProxMobile's Apps tab. Use this whenever he asks for an app, tool, tracker, "
        "game, calculator, etc. on the phone; don't write the app into the chat. "
        "create: a new app from `request` (+ optional short `name`). update: change app `app` as "
        "`request` says. rollback: rebuild app `app` from its older build `code`. These start in the "
        "background and return at once: tell Nathan it's building and that progress and the Install "
        "button are in the Apps tab (building takes a few minutes). status: one app's progress and "
        "builds. list: all apps. Only one build runs at a time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "rollback", "status", "list"]},
            "request": {"type": "string", "description": "What the app should do / what to change, in Nathan's words plus any details you know."},
            "name": {"type": "string", "description": "create: short app name (max 40 chars)."},
            "app": {"type": "string", "description": "update/rollback/status: the app id (from list)."},
            "code": {"type": "integer", "description": "rollback: the build number to go back to."},
        },
        "required": ["action"],
    },
}


# ---- context ----------------------------------------------------------------------------------

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
    return platform, platform in ALLOWED_PLATFORMS


def _log(rec: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("[proxai-apps] log failed: %s", exc)


# ---- reading the apps folder (read-only from the gateway) -------------------------------------

def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = re.sub(r"^[^a-z]+", "", s)[:32].strip("-")
    return s if len(s) >= 2 else "app"


def new_id(name: str) -> str:
    base, n = slug(name), 1
    cand = base
    while (APPS_HOST / cand).exists():
        n += 1
        cand = f"{base}-{n}"
    return cand


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def app_info(app_id: str) -> dict | None:
    d = APPS_HOST / app_id
    if not ID_RE.match(app_id) or not d.is_dir():
        return None
    meta = _read_json(d / "proxai-app.json", {})
    status = _read_json(d / "status.json", {})
    builds = _read_json(d / "builds.json", [])
    out = {"app": app_id, "name": meta.get("name", app_id), "description": meta.get("description", ""),
           "version": meta.get("version"), "state": status.get("state", "unknown"),
           "message": status.get("message", ""), "updated": status.get("time"),
           "builds": [{"code": b.get("code"), "version": b.get("version"), "time": b.get("time"),
                       "note": b.get("note", "")} for b in builds[-10:]]}
    if _job and _job["app"] == app_id:
        out["running"] = {"kind": _job["kind"], "for_s": int(time.time() - _job["started"])}
    if app_id in _last:
        out["last_job"] = _last[app_id]
    return out


def list_apps() -> list[dict]:
    if not APPS_HOST.is_dir():
        return []
    out = []
    for d in sorted(APPS_HOST.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and ID_RE.match(d.name):
            i = app_info(d.name)
            if i:
                out.append({k: i[k] for k in ("app", "name", "version", "state", "message", "updated")})
    return out


# ---- running the work in the sandbox ----------------------------------------------------------

_cc_module = None


def _cc():
    """The claude-code-delegation plugin (same runner, limits and log as the claude_code tool)."""
    global _cc_module
    if _cc_module is None:
        spec = importlib.util.spec_from_file_location(
            "proxai_apps_claude_code", HERE.parent / "claude-code-delegation" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _cc_module = mod
    return _cc_module


def _shell(command: str, task_id=None, timeout: int = 120) -> dict:
    """A short command in the sandbox (Hermes's terminal backend)."""
    from tools.terminal_tool import terminal_tool
    raw = terminal_tool(command=command, timeout=timeout, task_id=task_id)
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except ValueError:
        return {"output": str(raw)}


def build_task(kind: str, app_id: str, request: str, name: str = "") -> str:
    d = f"{APPS_SANDBOX}/{app_id}"
    head = (f"Build a ProxAI app for Nathan's phone. First read /opt/proxai-shell/GUIDE.md and follow it "
            f"exactly; the example app is in /opt/proxai-shell/example/proxai-test/.\n\n"
            f"App folder: {d}  (app id: {app_id}). Work only inside this folder.\n")
    if kind == "create":
        what = (f"This is a NEW app. Name: {name or 'pick a short name'}.\n"
                f"Nathan's request:\n{request}\n")
    else:
        what = (f"This app ALREADY EXISTS. Change it as Nathan asks, keep everything else working, keep its "
                f"saved data format compatible, and bump the version (e.g. 1.0 -> 1.1).\n"
                f"Nathan's change request:\n{request}\n")
    tail = (f"\nReport progress with `proxai-app-build --status planning|writing|testing \"short message\" {d}`. "
            f"Test the logic with Node. Finish with `proxai-app-build {d}` and fix problems until it prints "
            f"\"ok\": true. Your final answer: 2-3 plain sentences for Nathan (what the app does, how to use "
            f"it, version), no markdown.")
    return head + what + tail


def _run_job(kind: str, app_id: str, request: str, name: str, code: int | None, task_id, who: str) -> None:
    global _job
    started = time.time()
    d = f"{APPS_SANDBOX}/{app_id}"
    result: dict
    try:
        if kind == "rollback":
            r = _shell(f"proxai-app-build --rollback {int(code)} {d}", task_id=task_id, timeout=180)
            out = (r.get("output") or "").strip().splitlines()
            parsed = _read_last_json(out)
            result = {"ok": bool(parsed.get("ok")), "summary": f"rolled back to build {code}",
                      "error": parsed.get("error") or r.get("error")}
        else:
            _shell(f"mkdir -p {d} && proxai-app-build --status queued "
                   f"{shlex.quote('Starting: ' + (name or app_id))} {d}", task_id=task_id, timeout=60)
            raw = _cc()._handle_claude_code(
                {"task": build_task(kind, app_id, request, name), "project": "apps",
                 "max_turns": BUILD_TURNS, "timeout_minutes": BUILD_MINUTES},
                task_id=task_id, session_id=f"proxai-app-{app_id}")
            cc = json.loads(raw)
            status = _read_json(APPS_HOST / app_id / "status.json", {})
            ok = bool(cc.get("ok")) and status.get("state") == "built"
            result = {"ok": ok, "summary": (cc.get("result") or "")[:800],
                      "error": None if ok else (cc.get("error") or f"build ended in state {status.get('state')}")}
            if not ok and status.get("state") not in ("failed", "built"):
                _shell(f"proxai-app-build --status failed {shlex.quote(str(result['error'])[:200])} {d}",
                       task_id=task_id, timeout=60)
    except Exception as exc:  # never leave the lock held
        logger.exception("[proxai-apps] job failed")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result["duration_s"] = round(time.time() - started)
    _last[app_id] = {"kind": kind, **{k: v for k, v in result.items() if v is not None}}
    _log({"app": app_id, "kind": kind, "by": who, "request": request[:500], **result})
    with _lock:
        _job = None


def _read_last_json(lines: list[str]) -> dict:
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def _start(kind: str, app_id: str, request: str, name: str, code, task_id) -> str | None:
    """Start a background job; returns an error message if one is already running."""
    global _job
    with _lock:
        if _job is not None:
            return (f"Another app is being built right now ({_job['name']}, {_job['kind']}, "
                    f"{int(time.time() - _job['started'])}s). Try again when it's done.")
        _job = {"app": app_id, "kind": kind, "started": time.time(), "name": name or app_id}
    who = _session("HERMES_SESSION_USER_NAME") or os.getenv("USER", "nathan")
    ctx = contextvars.copy_context()   # same session context -> same sandbox container
    t = threading.Thread(target=ctx.run, args=(_run_job, kind, app_id, request, name, code, task_id, who),
                         name=f"proxai-app-{app_id}", daemon=True)
    _job["thread"] = t
    t.start()
    return None


# ---- tool -------------------------------------------------------------------------------------

def _handle(args: dict, task_id=None, session_id=None, **_kwargs) -> str:
    args = args or {}
    action = args.get("action", "")
    where, allowed = _where()

    if action == "list":
        return json.dumps({"ok": True, "apps": list_apps(),
                           "building": (_job or {}).get("app")})
    if action == "status":
        info = app_info(str(args.get("app", "")))
        return json.dumps({"ok": True, **info} if info else {"ok": False, "error": "no app with that id; use list"})

    if action not in ("create", "update", "rollback"):
        return json.dumps({"ok": False, "error": f"unknown action '{action}'"})
    if not allowed:
        return json.dumps({"ok": False, "error": f"Apps are built from the ProxMobile app, not {where}. "
                                                 "Tell Nathan to ask on his phone."})
    request = str(args.get("request") or "").strip()[:MAX_REQUEST]

    if action == "create":
        if not request:
            return json.dumps({"ok": False, "error": "request is empty: what should the app do?"})
        name = str(args.get("name") or "").strip()[:40]
        app_id = new_id(name or request[:40])
        err = _start("create", app_id, request, name, None, task_id)
        if err:
            return json.dumps({"ok": False, "busy": True, "error": err})
        return json.dumps({"ok": True, "started": True, "app": app_id,
                           "tell_nathan": f"Building {name or 'your app'} now; it takes a few minutes. Progress "
                                          "and the Install button are in the Apps tab."})

    app_id = str(args.get("app", ""))
    if not app_info(app_id):
        return json.dumps({"ok": False, "error": "no app with that id; use list"})
    if action == "update":
        if not request:
            return json.dumps({"ok": False, "error": "request is empty: what should change?"})
        err = _start("update", app_id, request, app_info(app_id)["name"], None, task_id)
        if err:
            return json.dumps({"ok": False, "busy": True, "error": err})
        return json.dumps({"ok": True, "started": True, "app": app_id,
                           "tell_nathan": "Updating it now; the Apps tab shows progress and an Update button when it's ready."})

    code = args.get("code")
    codes = [b["code"] for b in app_info(app_id)["builds"]]
    if not isinstance(code, int) or code not in codes:
        return json.dumps({"ok": False, "error": f"code must be one of this app's builds: {codes}"})
    err = _start("rollback", app_id, "", app_info(app_id)["name"], code, task_id)
    if err:
        return json.dumps({"ok": False, "busy": True, "error": err})
    return json.dumps({"ok": True, "started": True, "app": app_id,
                       "tell_nathan": f"Rolling back to build {code}; it shows up as a new version to install in the Apps tab."})


# ---- hint for app requests --------------------------------------------------------------------

_APP_ASK = re.compile(
    r"\b(build|make|create|generate|code|design|write)\b.{0,80}\b(apps?|application|tool|tracker|timer|"
    r"calculator|game|dashboard|widget|counter|planner|checklist|monitor|log)\b", re.I | re.S)
_APP_CHANGE = re.compile(
    r"\b(change|update|fix|add|remove|roll ?back|tweak|improve)\b.{0,80}\bapps?\b|\bapps?\b.{0,40}\b(roll ?back|build \d+)\b",
    re.I | re.S)
# "a grocery list I can check off" is an app; "make a list of groceries" is not.
_LIST_APP = re.compile(r"\b(build|make|create)\b.{0,40}\blist\b.{0,20}\b(i|we|you) can\b", re.I | re.S)
_IN_CHAT = re.compile(r"\b(quick|in (the|this) chat|right here|html (file|page)?)\b", re.I)
HINT = ("[proxai-apps] This is an app request from Nathan's phone. Use the `proxai_app` tool: it is "
        "behind tool_search (search for \"proxai_app\", then call it with tool_call). create = new app "
        "(short `name` + the full request with every detail); update / rollback = an app made that way "
        "(`list` shows the ids). Do NOT write the app's HTML in your reply. Once the build has started, "
        "answer in one or two sentences: it's building, and progress and the Install button are in "
        "ProxMobile's Apps tab.")


def _plain(user_message) -> str:
    if isinstance(user_message, list):
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    return re.sub(r"\[[^\]]*\]", " ", str(user_message or ""))


_QUESTION = re.compile(r"^\s*(how|what|why|which|when|where|should|would it|is it|are|do|does|did|explain|tell me)\b", re.I)
_LEAD = re.compile(r"^\s*(hey\s+)?(proxai[,:]?\s+)?((can|could|would|will) you\s+)?(please\s+)?"
                   r"(build|make|create|generate|code|design|write)\s+(me\s+|us\s+)?((a|an|my|the|some)\s+)?", re.I)
_CUT = re.compile(r"\s+(with|that|which|for|to|where|so|and|using|so that|(i|we|you) can)\b|[.,!?;:(]", re.I)


def guess_name(text: str) -> str:
    """'Build a pomodoro timer with a progress ring' -> 'Pomodoro Timer' ('' if unclear)."""
    rest = _LEAD.sub("", text.strip(), count=1)
    rest = _CUT.split(rest, maxsplit=1)[0]
    rest = re.sub(r"\b(app|application|apk)\b", "", rest, flags=re.I)
    words = [w for w in re.findall(r"[A-Za-z0-9']+", rest)][:4]
    name = " ".join(w if w.isupper() else w.capitalize() for w in words)
    return name[:40] if len(name) >= 3 else ""


def is_new_app_request(text: str) -> bool:
    if _IN_CHAT.search(text) or _QUESTION.search(text):
        return False
    if _APP_CHANGE.search(text) and not re.search(r"\b(new|another)\b", text, re.I):
        return False   # a change to an existing app: the model picks which one
    return bool(_APP_ASK.search(text) or _LIST_APP.search(text))


def _on_pre_llm_call(session_id=None, user_message=None, parent_session_id=None, **_kwargs):
    if parent_session_id:
        return None
    _where_now, allowed = _where()
    if not allowed:
        return None
    text = _plain(user_message).strip()
    if is_new_app_request(text):
        name = guess_name(text)
        app_id = new_id(name or text[:40])
        err = _start("create", app_id, text[:MAX_REQUEST], name, None, None)
        if err:
            return {"context": f"[proxai-apps] Nathan asked for a new app, but it can't start yet: {err} "
                               "Tell him that in one sentence. Don't write the app here."}
        _log({"app": app_id, "kind": "create", "by": "hook", "request": text[:500], "started": True})
        return {"context": f"[proxai-apps] Done for you: the app builder (Claude Code on the PC) has ALREADY "
                           f"started building this as \"{name or app_id}\" (app id {app_id}). Do NOT write the app "
                           "or any HTML, and do NOT call proxai_app create. Reply in one or two short sentences: "
                           "it's building now, takes a few minutes, and progress plus the Install button are in "
                           "ProxMobile's Apps tab."}
    if _APP_CHANGE.search(text) and not _IN_CHAT.search(text) and not _QUESTION.search(text):
        return {"context": HINT}
    return None


def register(ctx) -> None:
    ctx.register_tool(name="proxai_app", toolset="proxai_apps", schema=SCHEMA, handler=_handle, emoji="📱",
                      description="Build phone apps (APKs) with Claude Code; installed from ProxMobile's Apps tab")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
