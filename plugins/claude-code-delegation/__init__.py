"""
Claude Code delegation for the ProxAI profile (checklist Phase 7).

* ``claude_code`` tool: runs ``cc-run`` (baked into the proxai-sandbox image) through
  Hermes's own terminal backend, so Claude Code only ever runs inside the Docker
  sandbox, in ``/workspace/proxai/<project>`` (= ``~/dev/proxai/<project>`` on the host).
  The task travels as base64 on stdin, never as shell text. Turn and time caps are
  set here and clamped again by cc-run.
* The run is started as a tracked background process and this tool waits for it itself.
  Hermes's foreground terminal refuses to wait longer than 600 s and would return
  "Background process started" at once, which looked like a failure while Claude Code
  kept going. /stop or a new message stops the run, like a foreground command, and the
  whole run is killed inside the sandbox (Hermes's own kill only stops the outer shell).
* Every run is logged host-side to ``~/.local/state/proxai/claude-code.log`` (JSONL,
  out of the container's reach) and as one line in #bot-audit-log.
* Which channels may call it is decided by the discord-routing guard
  (``routing.json`` → ``tool_policy``), not here.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_PATH = Path.home() / ".local/state/proxai/claude-code.log"
_AUDIT_CHANNEL = "bot-audit-log"
_TOOLS_RE = re.compile(r"^(Read|Write|Edit|Glob|Grep)(,(Read|Write|Edit|Glob|Grep))*$")   # cc-run --tools (plugins only)
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MODELS = {"sonnet", "opus"}   # cc-run --model (other plugins may ask for one; the tool schema doesn't offer it)
_MAX_TASK_CHARS = 20000
_DEFAULT_TURNS, _MAX_TURNS = 25, 60
_DEFAULT_MINUTES, _MAX_MINUTES = 15, 30
_GRACE_S = 60          # extra time past cc-run's own limit before the run is killed here
_WAIT_SLICE_S = 120    # process_registry.wait() is clamped to TERMINAL_TIMEOUT; wait in slices
_LOG_TAIL_LINES = 40   # cc-run's result is its last output line
_TAG_VAR = "PROXAI_CC_RUN"  # marks every process of one run, for stopping it

_gateway = None  # captured on the first Discord message, used for audit posts

SCHEMA = {
    "name": "claude_code",
    "description": (
        "Hand heavy work to Claude Code, a coding agent that runs inside the sandbox in its own "
        "project folder (~/dev/proxai/<project>). Use it for writing or changing code, multi-file "
        "work, builds and tests, and long multi-step jobs. Do NOT use it for simple questions, "
        "chat, or saving a note. Claude Code can reach only a few allow-listed sites (Anthropic, "
        "npm, PyPI, GitHub read-only); it cannot see the vault or the rest of the computer. "
        "Give it a complete, self-contained task: it does not see this conversation. "
        "Returns its final answer and whether it succeeded. Report failures honestly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Full instructions for Claude Code, including what done looks like."},
            "project": {"type": "string", "description": "Project folder name under ~/dev/proxai (letters, digits, . _ -). Created if missing."},
            "max_turns": {"type": "integer", "description": f"Optional turn cap (default {_DEFAULT_TURNS}, max {_MAX_TURNS})."},
            "timeout_minutes": {"type": "integer", "description": f"Optional time limit (default {_DEFAULT_MINUTES}, max {_MAX_MINUTES})."},
        },
        "required": ["task", "project"],
    },
}


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "")
    except Exception:
        return ""


def _clamp(value, default: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def _parse_cc_output(output: str) -> dict:
    """Last JSON object line in cc-run's output (Claude Code's result or cc-run's error)."""
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def _log(record: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("[claude-code] could not write %s: %s", _LOG_PATH, exc)


def _post_audit(text: str) -> None:
    """Schedule one line in #bot-audit-log on the Discord client's loop (fire and forget)."""
    gw = _gateway
    if gw is None:
        return
    try:
        from gateway.config import Platform
        adapter = getattr(gw, "adapters", {}).get(Platform.DISCORD)
    except Exception:
        adapter = None
    client = getattr(adapter, "_client", None)
    loop = getattr(client, "loop", None)
    if client is None or loop is None:
        return

    async def _send():
        try:
            for guild in client.guilds:
                for channel in guild.text_channels:
                    if channel.name == _AUDIT_CHANNEL:
                        await channel.send(text[:1900])
                        return
        except Exception as exc:
            logger.debug("[claude-code] audit post failed: %s", exc)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop)
    except Exception as exc:
        logger.debug("[claude-code] audit scheduling failed: %s", exc)


def _reap_command(tag: str) -> str:
    """Shell that TERMs, then KILLs, every process whose environment carries the run's tag."""
    find = (f"grep -lsz '^{_TAG_VAR}={tag}$' /proc/[0-9]*/environ 2>/dev/null"
            " | cut -d/ -f3 | grep -vx \"$$\"")
    return (f"pids=$({find}); [ -n \"$pids\" ] && kill -TERM $pids 2>/dev/null; sleep 3; "
            f"pids=$({find}); [ -n \"$pids\" ] && kill -KILL $pids 2>/dev/null; true")


def _stop_run(registry, proc_id: str, tag: str, source: str) -> None:
    session = registry.get(proc_id)
    env = getattr(session, "env_ref", None)
    registry.kill_process(proc_id, source=source)
    if env is not None:  # sandbox run: kill the rest of the tree inside the container
        try:
            env.execute(_reap_command(tag), timeout=20)
        except Exception as exc:
            logger.warning("[claude-code] could not stop run %s in the sandbox: %s", proc_id, exc)


def _run_and_wait(command: str, limit_s: int, *, task_id=None, session_id=None) -> dict:
    """Start ``command`` as a tracked background process in the sandbox and block until it
    exits. Returns {"exit_code", "output"} or {"exit_code", "error"}.

    cc-run enforces the real time limit; ``limit_s`` (that limit + grace) is a backstop
    after which the run is killed here."""
    from tools.terminal_tool import terminal_tool
    from tools.process_registry import process_registry

    # Every process of this run (cc-run, timeout, claude and whatever claude starts) inherits
    # the tag, so a stop can find them all.
    tag = uuid.uuid4().hex
    raw = terminal_tool(command=f"export {_TAG_VAR}={tag}; {command}", background=True,
                        notify_on_complete=False, timeout=limit_s, task_id=task_id, session_id=session_id)
    try:
        spawn = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except ValueError:
        spawn = {"output": str(raw)}
    proc_id = spawn.get("session_id")
    if spawn.get("error") or not proc_id:
        return {"exit_code": spawn.get("exit_code"),
                "error": spawn.get("error") or f"could not start Claude Code: {str(spawn.get('output'))[:500]}"}

    deadline = time.monotonic() + limit_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_run(process_registry, proc_id, tag, "claude_code.deadline")
            return {"exit_code": 124, "error": f"Claude Code did not finish within {limit_s}s and was stopped"}
        res = process_registry.wait(proc_id, timeout=max(1, int(min(remaining, _WAIT_SLICE_S))))
        status = res.get("status")
        if status == "exited":
            log = process_registry.read_log(proc_id, limit=_LOG_TAIL_LINES)
            return {"exit_code": res.get("exit_code"), "output": log.get("output") or res.get("output") or ""}
        if status == "interrupted":
            if res.get("process_running"):
                continue  # a steer message only releases the wait; keep waiting like a foreground run
            _stop_run(process_registry, proc_id, tag, "claude_code.interrupted")
            return {"exit_code": 130, "error": "stopped: /stop or a new message arrived while Claude Code was running"}
        if status != "timeout":
            return {"exit_code": None, "error": f"lost track of the Claude Code run ({status}): {res.get('error', '')}"}


def _handle_claude_code(args: dict, task_id: str | None = None, session_id: str | None = None) -> str:
    task = str(args.get("task") or "").strip()
    project = str(args.get("project") or "").strip()
    turns = _clamp(args.get("max_turns"), _DEFAULT_TURNS, _MAX_TURNS)
    minutes = _clamp(args.get("timeout_minutes"), _DEFAULT_MINUTES, _MAX_MINUTES)
    model = str(args.get("model") or "").strip().lower()
    tools = str(args.get("tools") or "").replace(" ", "")
    # chat_name looks like "Server / #channel / thread title"; keep just the channel.
    chat_name = _session("HERMES_SESSION_CHAT_NAME")
    hashed = [p.strip()[1:] for p in chat_name.split(" / ") if p.strip().startswith("#")]
    channel = (hashed[0] if hashed else chat_name) or _session("HERMES_SESSION_PLATFORM") or "unknown"
    user = _session("HERMES_SESSION_USER_NAME") or "unknown"

    def refuse(msg: str) -> str:
        return json.dumps({"ok": False, "error": msg})

    if not task:
        return refuse("task is empty")
    if len(task) > _MAX_TASK_CHARS:
        return refuse(f"task is longer than {_MAX_TASK_CHARS} characters; summarise it")
    if not _PROJECT_RE.match(project):
        return refuse("project must be one folder name (letters, digits, . _ -) under ~/dev/proxai")
    if model and model not in _MODELS:
        return refuse(f"model must be one of {sorted(_MODELS)}")
    if tools and not _TOOLS_RE.match(tools):
        return refuse("tools must be a comma list of Read, Write, Edit, Glob, Grep")

    payload = base64.b64encode(task.encode("utf-8")).decode("ascii")
    seconds = minutes * 60
    command = (f"printf %s {payload} | base64 -d | "
               f"cc-run --project {project} --max-turns {turns} --timeout {seconds}"
               + (f" --model {model}" if model else "") + (f" --tools {tools}" if tools else ""))

    started = time.time()
    term = _run_and_wait(command, seconds + _GRACE_S, task_id=task_id, session_id=session_id)
    elapsed = round(time.time() - started, 1)

    exit_code = term.get("exit_code")
    output = term.get("output") or ""
    cc = _parse_cc_output(output)
    error = cc.get("cc_run_error") or term.get("error")
    ok = exit_code == 0 and bool(cc) and not cc.get("is_error") and not error
    if not ok and not error:
        error = cc.get("result") or (output[-800:] if output else f"exit code {exit_code}")

    # Hermes treats any result text containing "error" as a failed call, so the key is
    # only present when the run really failed.
    result = {"ok": ok, "success": ok, "project": project, "folder_on_host": f"~/dev/proxai/{project}"}
    if ok:
        result["result"] = cc.get("result", "")
    else:
        result["error"] = str(error)[:2000]
    result.update({"num_turns": cc.get("num_turns"), "duration_s": elapsed, "exit_code": exit_code})

    _log({
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "channel": channel, "user": user,
        "session_id": session_id, "project": project, "max_turns": turns, "timeout_s": seconds,
        "task": task[:2000], "ok": ok, "exit_code": exit_code, "num_turns": cc.get("num_turns"),
        "duration_s": elapsed, "cc_session_id": cc.get("session_id"),
        "error": result.get("error", "")[:500], "result_preview": result.get("result", "")[:500],
    })
    mark = "✅" if ok else "❌"
    _post_audit(f"🤖 Claude Code {mark} `#{channel}` · **{user}** · project `{project}` · "
                f"{elapsed}s · turns {cc.get('num_turns', '?')} · task: {task[:200]}")
    return json.dumps(result, ensure_ascii=False)


async def _on_pre_gateway_dispatch(event=None, gateway=None, **_kwargs):
    """Remember the gateway so tool runs can post to #bot-audit-log. Never changes the event."""
    global _gateway
    source = getattr(event, "source", None)
    if source is not None and gateway is not None \
            and getattr(source.platform, "value", source.platform) == "discord":
        _gateway = gateway
    return None


def register(ctx) -> None:
    ctx.register_tool(name="claude_code", toolset="claude_code", schema=SCHEMA,
                      handler=_handle_claude_code, emoji="🤖",
                      description="Delegate heavy coding / multi-step work to Claude Code in the sandbox")
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
