"""
ProxAI vault writes with Nathan's permission.

Hermes's container sees the vault read-only except _inbox/, and the gateway's own
sandbox keeps it read-only too. This plugin is the one way to write elsewhere:

* ``vault_write`` tool: write (create / replace / append a note), confirm, cancel, status.
* Folder rules come from ``policy.json``:
    free  = written right away (_inbox, 05-Brain-Dump)
    never = refused (01-Personal, 07-Security, 10-Wiki, AGENTS.md, CLAUDE.md, anything starting with ".");
            10-Wiki is written only by the wiki synthesizer (plugins/proxai-wiki), never by chat
    ask   = every other folder: Hermes asks Nathan, and the ``pre_llm_call`` hook reads his NEXT
            message (typed, phone, or voice-note transcript). A plain yes arms the write; anything
            else cancels it. ``confirm`` only works once armed, so the model can't approve itself.
            Requests expire after 10 minutes.
* "ask" writes only from Nathan's DMs, the phone app (api_server) or the local CLI. Channels don't
  get the tool at all unless routing.json's tool_policy lists it.
* The write runs as a separate sandboxed systemd user job (apply.py), which checks the rules
  again, backs up any file it changes and takes the shared vault lock.
* Every write and refusal goes to the security log (07-Security/ProxAI-Audit, ``VAULT-WRITE``);
  approved writes are also posted to #bot-audit-log.
* ``/vault-writes`` command: the folder rules and the last few writes.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_rules as rules  # noqa: E402

_CONFIRM_SECS = 600
_APPLY_TIMEOUT = 60
_AUDIT_CHANNEL = "bot-audit-log"
_ALLOWED_PLATFORMS = {"api_server", "cli"}   # plus Discord DMs
_PREVIEW_CHARS = 160

_gateway = None
_pending: dict[str, dict] = {}   # session_id -> request (+ "created", "confirmed")


SCHEMA = {
    "name": "vault_write",
    "description": (
        "Write a note into Nathan's Obsidian vault on the host (the container's vault is read-only "
        "outside _inbox/). Paths are relative to the vault, e.g. "
        "'04-Knowledge/rust-notes.md'. Folders _inbox and 05-Brain-Dump are written right "
        "away. Every other folder needs Nathan's permission: action=write returns a question; ask "
        "him exactly that and STOP; the system reads his reply. confirm: only after the system "
        "tells you he said yes. 01-Personal, 07-Security, 10-Wiki (the wiki synthesizer writes it: "
        "Nathan says 'update the wiki'), AGENTS.md and CLAUDE.md can never be written. how: create (new note, fails if it exists), replace (whole note; the old one is "
        "backed up), append (add to the end). Never claim a note was saved unless the result says "
        "ok. status: the folder rules and any pending request. cancel: drop a pending request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["write", "confirm", "cancel", "status"]},
            "path": {"type": "string", "description": "Vault-relative path of the note (write)."},
            "content": {"type": "string", "description": "Full note text (Markdown) for write."},
            "how": {"type": "string", "enum": list(rules.HOWS), "description": "Default: create."},
        },
        "required": ["action"],
    },
}


# ---- context (same as proxai-mode) ------------------------------------------------------------

def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        return os.getenv(name, "")


def _where() -> tuple[str, bool]:
    """('dm' | 'api_server' | 'cli' | '#channel' | 'cron', may ask Nathan here?)"""
    if _session("HERMES_CRON_SESSION"):
        return "cron", False
    platform = _session("HERMES_SESSION_PLATFORM") or "cli"
    if platform == "discord":
        if _session("HERMES_SESSION_CHAT_TYPE") == "dm":
            return "dm", True
        return "#" + (_session("HERMES_SESSION_CHAT_NAME") or "channel"), False
    return platform, platform in _ALLOWED_PLATFORMS


def _who() -> str:
    return _session("HERMES_SESSION_USER_NAME") or os.getenv("USER", "unknown")


def _session_id() -> str:
    return _session("HERMES_SESSION_ID") or os.getenv("HERMES_SESSION_ID", "")


# ---- audit ------------------------------------------------------------------------------------

_security_log = None


def _audit(text: str, where: str) -> None:
    """One VAULT-WRITE line in the security log, written by discord-routing's own SecurityLog."""
    global _security_log
    try:
        if _security_log is None:
            spec = importlib.util.spec_from_file_location(
                "proxai_vault_write_guard", HERE.parent / "discord-routing" / "guard.py")
            guard = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(guard)
            _security_log = guard.SecurityLog(rules.vault_dir())
        _security_log.write("VAULT-WRITE", where, text)
    except Exception as exc:
        logger.warning("[vault-write] security log failed: %s", exc)


def _post_audit(text: str) -> None:
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
            logger.debug("[vault-write] audit post failed: %s", exc)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop)
    except Exception as exc:
        logger.debug("[vault-write] audit scheduling failed: %s", exc)


# ---- the write job ----------------------------------------------------------------------------

def _work() -> Path:
    return rules.state_dir() / "vault-write"


def _sandbox_props() -> list[str]:
    vault, state = rules.vault_dir(), rules.state_dir()
    hidden = " ".join(f"-{vault / d}" for d in ("01-Personal", "07-Security", ".git", ".obsidian"))
    props = ["ProtectHome=tmpfs", "ProtectSystem=strict", "PrivateTmp=yes", "NoNewPrivileges=yes",
             "PrivateNetwork=yes", f"BindPaths={vault} {state}", f"BindReadOnlyPaths={HERE}",
             f"InaccessiblePaths={hidden}"]
    return [x for p in props for x in ("-p", p)]


def _run_apply(req: dict) -> dict:
    """Hand the request to apply.py as its own sandboxed systemd user job and wait for it."""
    work = _work()
    (work / "requests").mkdir(parents=True, exist_ok=True)
    req_file = work / "requests" / f"{req['id']}.json"
    fd = os.open(req_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(req, f)
    python = shutil.which("python3") or "/usr/bin/python3"
    argv = ["systemd-run", "--user", "--wait", "--collect", "--quiet",
            f"--unit=proxai-vault-write-{req['id']}", *_sandbox_props(),
            "-E", f"PROXAI_VAULT={rules.vault_dir()}", "-E", f"PROXAI_STATE_DIR={rules.state_dir()}",
            python, str(HERE / "apply.py"), str(req_file)]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=_APPLY_TIMEOUT)
    except Exception as exc:
        return {"ok": False, "error": f"couldn't start the write job: {exc}"}
    try:
        return json.loads((work / "results" / f"{req['id']}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "error": "the write job didn't report back (see journalctl --user -u "
                                      f"proxai-vault-write-{req['id']})"}


# ---- tool -------------------------------------------------------------------------------------

def _describe(req: dict) -> str:
    """What the write will do, in words, for Nathan's yes/no."""
    rel, how, content = req["path"], req["how"], req["content"]
    target = rules.vault_dir() / rel
    new_lines = len(content.splitlines())
    if how == "replace" and target.is_file():
        try:
            old_lines = len(target.read_text(encoding="utf-8").splitlines())
        except OSError:
            old_lines = "?"
        what = f"replace {rel} ({old_lines} lines now → {new_lines} lines; the old note is backed up)"
    elif how == "append":
        what = f"add {new_lines} line{'s' * (new_lines != 1)} to the end of {rel}"
    else:
        what = f"create {rel} ({new_lines} line{'s' * (new_lines != 1)})"
    preview = " ".join(content.split())[:_PREVIEW_CHARS]
    if len(" ".join(content.split())) > _PREVIEW_CHARS:
        preview += "…"
    return f"{what}. It starts: \"{preview}\""


def _short(result: dict) -> str:
    if result.get("ok"):
        verb = "created" if result.get("created") else ("appended to" if result.get("how") == "append" else "replaced")
        return f"{verb} {result['path']}"
    return f"refused: {result.get('error', '?')}"


def _status() -> dict:
    p = rules.load_policy()
    out = {"ok": True, "free_folders": p["free"], "never": p["never"],
           "other_folders": "need Nathan's yes (DMs or phone app)", "file_types": p["extensions"]}
    pend = _pending.get(_session_id())
    if pend:
        out["pending"] = {"path": pend["path"], "how": pend["how"], "confirmed": pend["confirmed"]}
    return out


def _handle(args: dict, **_kwargs) -> str:
    args = args or {}
    action = args.get("action", "")
    where, allowed = _where()
    sid = _session_id()

    if action == "status":
        return json.dumps(_status())

    if action == "cancel":
        p = _pending.pop(sid, None)
        return json.dumps({"ok": True, "cancelled": bool(p)})

    if action == "write":
        if where == "cron":
            return json.dumps({"ok": False, "error": "Scheduled jobs can't write to the vault."})
        how = args.get("how") or "create"
        try:
            rel, rule = rules.check(args.get("path", ""), args.get("content", ""), how, rules.load_policy())
        except rules.Refused as exc:
            _audit(f"refused {args.get('path', '')!r}: {exc}", where)
            return json.dumps({"ok": False, "refused": True, "error": str(exc)})
        req = {"id": uuid.uuid4().hex[:12], "path": rel, "how": how, "content": str(args.get("content", "")),
               "rule": rule, "who": _who(), "where": where, "approved_by": None}
        if rule == "free":
            result = _run_apply(req)
            _audit(f"{_short(result)} (free folder) · by {req['who']}", where)
            return json.dumps({k: v for k, v in result.items() if k != "backup"})
        if not allowed:
            return json.dumps({"ok": False, "error": f"Writing to {rel.split('/')[0]} needs Nathan's yes, and "
                                                     f"that only works in his DMs or the phone app, not {where}. "
                                                     "Save it to _inbox instead, or ask him there."})
        if not sid:
            return json.dumps({"ok": False, "error": "no session id; can't track Nathan's answer"})
        _pending[sid] = {**req, "created": time.time(), "confirmed": False}
        return json.dumps({
            "ok": True, "needs_confirmation": True,
            "ask_nathan": f"Can I {_describe(req)}? Say yes to allow.",
            "instructions": "Ask Nathan this and end your reply. Do NOT call confirm now: the system "
                            "reads his next message and tells you if he said yes.",
        })

    if action == "confirm":
        p = _pending.get(sid)
        if not p:
            return json.dumps({"ok": False, "error": "No pending vault write. Use write first."})
        if time.time() - p["created"] > _CONFIRM_SECS:
            _pending.pop(sid, None)
            return json.dumps({"ok": False, "error": "The request expired. Ask again with write."})
        if not p["confirmed"]:
            return json.dumps({"ok": False, "error": "Nathan hasn't said yes yet. Ask him and wait for his reply."})
        _pending.pop(sid, None)
        req = {k: v for k, v in p.items() if k not in ("created", "confirmed")}
        req["approved_by"] = f"{p['who']}@{p['where']}"
        result = _run_apply(req)
        _audit(f"{_short(result)} · approved by {req['approved_by']}", p["where"])
        icon = "📝" if result.get("ok") else "❌"
        _post_audit(f"{icon} Vault write ({req['approved_by']}): {_short(result)}")
        return json.dumps({k: v for k, v in result.items() if k != "backup"})

    return json.dumps({"ok": False, "error": f"unknown action '{action}'"})


# ---- hooks ------------------------------------------------------------------------------------

_YES = re.compile(
    r"^(?:(?:yes|yeah|yea|yep|yup|ya|sure|ok|okay|confirm|confirmed|approve|approved|affirmative|"
    r"do it|go ahead|go for it|save it|write it|send it|let'?s go|please|bro|man|ye|yay|now)\b[\s,.!]*)+$")
_NO_WORDS = re.compile(r"\b(no|nah|nope|don'?t|do not|not|wait|cancel|stop|hold on|never)\b")


def _plain_text(user_message) -> str:
    if isinstance(user_message, list):   # multimodal content parts
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    text = str(user_message or "")
    text = re.sub(r"\[[^\]]*\]", " ", text)                 # gateway notes like [Voice channel now: ...]
    text = text.lower().replace("’", "'")
    text = re.sub(r"[\"“”*_`~]", " ", text)                  # voice transcripts arrive quoted
    return " ".join(text.split()).strip(" .!,")


def is_yes(user_message) -> bool:
    text = _plain_text(user_message)
    return bool(text) and len(text) <= 60 and not _NO_WORDS.search(text) and bool(_YES.match(text))


# Hermes hides plugin tools behind tool_search, and the local model rarely looks there. So it
# reaches for write_file on the read-only vault. These two hooks point it at vault_write instead.
_CONTAINER_VAULT = "/workspace/vault/"
_CONTAINER_WRITABLE = ("_inbox",)                 # mounted read-write in the container already
_FILE_TOOLS = {"write_file", "patch"}
_SAVE_ASK = re.compile(
    r"\b(add|save|put|write|jot|note|log|capture|dump|file|store|record)\b.{0,80}?"
    r"\b(brain ?dump|vault|wiki|inbox|obsidian|my notes|a note|notes? folder)\b", re.S)


def _call_example(rel: str) -> str:
    args = json.dumps({"action": "write", "path": rel, "content": "<the note in Markdown>"})
    return f'tool_call({{"calls": [{{"name": "vault_write", "arguments": {args}}}]}})'


def _on_pre_tool_call(tool_name: str = "", args=None, **_kwargs):
    """write_file/patch into a read-only vault folder: block it and say how to use vault_write."""
    if tool_name not in _FILE_TOOLS or not isinstance(args, dict):
        return None
    raw = str(args.get("path") or args.get("file_path") or "")
    if not raw.startswith(_CONTAINER_VAULT):
        return None
    try:
        rel = rules.normalise(raw)
    except rules.Refused:
        return None
    if rel.split("/")[0] in _CONTAINER_WRITABLE:
        return None
    rule = rules.rule_for(rel, rules.load_policy())
    if rel.split("/")[0] == "10-Wiki":
        return {"action": "block", "message": "Not written: the wiki is written only by the wiki synthesizer "
                                              "(Claude Code). Tell Nathan he can say \"update the wiki\", or save "
                                              "the note to _inbox/ and the wiki picks it up from there."}
    if rule == "never":
        return {"action": "block", "message": f"{rel} can never be written by ProxAI. Tell Nathan."}
    _where_now, can_ask = _where()
    if rule == "ask" and not can_ask:
        return {"action": "block", "message": f"The vault is read-only here, and {rel.split('/')[0]} needs "
                                              "Nathan's yes, which only works in his DMs or the phone app. "
                                              "Save it to _inbox/ instead (write_file works there)."}
    after = ("It's written right away." if rule == "free" else
             "That folder needs Nathan's permission: vault_write returns a question, ask him exactly that "
             "and stop.")
    return {"action": "block",
            "message": f"Not written: the vault is read-only in the sandbox. Save notes with the vault_write "
                       f"tool instead: {_call_example(rel)}. {after} Never say a note was saved unless "
                       "vault_write returns ok."}


def _save_hint(user_message) -> dict | None:
    """Nathan asks to save something to the vault: name the tool so the model doesn't guess."""
    text = _plain_text(user_message)
    if not text or not _SAVE_ASK.search(text) or not _where()[1]:
        return None
    return {"context": "[vault-write] To save this in the vault use the vault_write tool (not write_file, the "
                       f"vault is read-only in the sandbox), e.g. {_call_example('05-Brain-Dump/<short-name>.md')}. "
                       "05-Brain-Dump and _inbox are written right away; other folders need his yes; never 10-Wiki "
                       "(the wiki synthesizer writes it). "
                       "Report what the result says."}


def _on_pre_llm_call(session_id=None, user_message=None, parent_session_id=None, **_kwargs):
    if parent_session_id:
        return None
    p = _pending.get(session_id or "")
    if not p:
        return _save_hint(user_message)
    if time.time() - p["created"] > _CONFIRM_SECS:
        _pending.pop(session_id, None)
        return {"context": f"[vault-write] The request to write {p['path']} expired; nothing was written."}
    if p["confirmed"]:
        return None
    if is_yes(user_message):
        p["confirmed"] = True
        return {"context": f"[vault-write] Nathan said yes to writing {p['path']}. Call vault_write with "
                           "action=confirm now, then report the result."}
    _pending.pop(session_id, None)
    _audit(f"declined {p['path']} (reply wasn't a plain yes)", p["where"])
    return {"context": f"[vault-write] Nathan did not approve (his reply wasn't a plain yes), so {p['path']} "
                       "was NOT written. Don't write it; ask again only if he wants."}


async def _on_pre_gateway_dispatch(event=None, gateway=None, **_kwargs):
    """Remember the gateway so approved writes can be posted to #bot-audit-log."""
    global _gateway
    source = getattr(event, "source", None)
    if source is not None and gateway is not None \
            and getattr(source.platform, "value", source.platform) == "discord":
        _gateway = gateway
    return None


def _cmd_vault_writes(raw_args: str = "") -> str:
    p = rules.load_policy()
    lines = ["📝 **Vault writes**",
             f"Free (no asking): {', '.join(p['free'])}",
             f"Never: {', '.join(p['never'])}",
             "Everything else: I ask you first; only a plain yes lets it through (DMs or phone)."]
    try:
        recent = (_work() / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()[-5:]
    except OSError:
        recent = []
    if recent:
        lines.append("Last writes:")
        for line in recent:
            try:
                r = json.loads(line)
                lines.append(f"· {r.get('time', '?').replace('T', ' ')} {_short(r)}")
            except ValueError:
                continue
    return "\n".join(lines)


def register(ctx) -> None:
    ctx.register_tool(name="vault_write", toolset="proxai_vault", schema=SCHEMA, handler=_handle,
                      emoji="📝", description="Write vault notes; folders outside _inbox/brain dump/wiki need Nathan's yes")
    ctx.register_command("vault-writes", _cmd_vault_writes, description="Vault write rules and the last writes")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
