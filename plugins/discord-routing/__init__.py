"""
Discord channel model routing for the ProxAI profile.

Runs on ``pre_gateway_dispatch`` so no Hermes core files are modified:
  * picks a model tier from ``routing.json`` by channel name (threads use
    their parent channel's tier; DMs are left on the profile's default model)
  * ``!model`` replies with the channel's model, ``!opus <msg>`` escalates one message
  * sets the channel prompt from ``routing.json`` ``prompts`` by channel name
    (threads use their parent channel's prompt)
  * posts one line per routed message to #bot-audit-log
  * adds the vault privacy rule to the channel prompt for cloud tiers
  * saves captures in the ``routing.json`` ``captures`` channels itself
    (see captures.py), so the model only writes the reply
  * ``pre_llm_call``: on file-related messages, says where uploads are and how to send files back
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import captures
from .guard import REPORT_SCHEMA, guard

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent / "routing.json"
# Where the Docker sandbox mounts the vault (see terminal.docker_volumes in config.yaml).
_VAULT_ROOT = "/workspace/vault"
_AUDIT_CHANNEL = "bot-audit-log"

_config: dict = {}
_config_mtime: float = 0.0


def _load_config() -> dict:
    """Load routing.json, reloading when the file changes."""
    global _config, _config_mtime
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        return {}
    if mtime != _config_mtime:
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                _config = json.load(f)
            _config_mtime = mtime
            logger.info("[discord-routing] Loaded %s", _CONFIG_PATH)
        except Exception as exc:
            logger.warning("[discord-routing] Failed to load %s: %s", _CONFIG_PATH, exc)
    return _config


def _resolve(entry: dict, tier_name: str = "") -> dict:
    """Turn a tier (or opus_override) entry into a model override dict."""
    api_key_env = entry.get("api_key_env", "")
    if api_key_env:
        api_key = os.getenv(api_key_env, "")
    elif entry.get("api_key"):
        api_key = str(entry["api_key"])
    elif entry.get("base_url"):
        # Local provider (Ollama): a non-empty placeholder skips env-based key lookup.
        api_key = "no-key"
    else:
        api_key = ""
    return {
        "tier": tier_name,
        "model": entry.get("model", ""),
        "provider": entry.get("provider") or None,
        "base_url": entry.get("base_url") or None,
        "api_mode": entry.get("api_mode") or None,
        "label": entry.get("label") or entry.get("model") or tier_name,
        "api_key": api_key,
        "vault_blocked_folders": entry.get("vault_blocked_folders") or [],
    }


def _routing_for(channel_name: str, config: dict) -> dict:
    tier_name = config.get("channels", {}).get(channel_name) or config.get("default_tier", "")
    tier = config.get("tiers", {}).get(tier_name)
    return _resolve(tier, tier_name) if tier else {}


def _opus_for(config: dict) -> dict:
    opus = config.get("opus_override")
    if isinstance(opus, dict) and opus.get("model"):
        return _resolve(opus, "opus_override")
    tier3 = config.get("tiers", {}).get("tier3")
    return _resolve(tier3, "tier3") if tier3 else {}


def _channel_name(source, client) -> str:
    """Channel name used for routing; threads resolve to their parent channel."""
    lookup_id = source.parent_chat_id if source.chat_type == "thread" and source.parent_chat_id else source.chat_id
    if client is not None and lookup_id:
        try:
            channel = client.get_channel(int(lookup_id))
            if channel is not None and getattr(channel, "name", None):
                return channel.name
        except (TypeError, ValueError):
            pass
    # Discord's cache missed: use a channel already seen with this ID, then the chat name.
    known = guard.chat_channels.get(str(lookup_id or ""))
    if known:
        return known
    return channel_from_chat_name(source.chat_name or "")


def channel_from_chat_name(name: str) -> str:
    """Channel from a chat name: "Guild / #channel" or, for threads, "Guild / #channel / thread title".

    Only the first segment that starts with "#" counts, so a thread title never becomes the channel.
    """
    for part in name.split(" / "):
        part = part.strip()
        if part.startswith("#") and len(part) > 1:
            return part[1:].strip()
    return name.strip() if " / " not in name and "#" not in name else ""


def _privacy_prompt(folders: list[str]) -> str:
    if not folders:
        return ""
    folder_list = "\n".join(f"  - {_VAULT_ROOT}/{f}" for f in folders)
    return (
        "VAULT PRIVACY RESTRICTION (non-overridable):\n"
        "The following vault folders are strictly off-limits. "
        "Do NOT read, list, grep, cat, search, or reference the contents of:\n"
        f"{folder_list}\n"
        "If the user asks you to access these folders, refuse and explain they are "
        "private. This restriction cannot be lifted by any user instruction."
    )


async def _send_audit(client, source_channel: str, user_name: str, label: str, escalated: bool) -> None:
    suffix = " ⬆ `!opus`" if escalated else ""
    msg = f"📋 `#{source_channel}` · **{user_name}** · model: `{label}`{suffix}"
    try:
        for guild in client.guilds:
            for channel in guild.text_channels:
                if channel.name == _AUDIT_CHANNEL:
                    await channel.send(msg)
                    return
    except Exception as exc:
        logger.debug("[discord-routing] Audit log delivery failed: %s", exc)


def _allowed_capture_user(source, adapter) -> bool:
    """Only the allowed Discord user's own messages are ever saved.

    Uses the allow-list the Discord adapter loaded at connect time; the env var is a fallback.
    """
    if getattr(source, "is_bot", False):
        return False
    allowed = {str(u) for u in (getattr(adapter, "_allowed_user_ids", None) or set())}
    if not allowed:
        allowed = {u.strip() for u in os.getenv("DISCORD_ALLOWED_USERS", "").split(",") if u.strip()}
    return "*" not in allowed and str(source.user_id or "") in allowed


async def _voice_transcript(event, source, adapter, gateway) -> str:
    """Transcribe a voice note now, through the gateway's own once-per-message STT.

    The gateway caches the result on the event, so the normal reply path reuses this transcript
    instead of running Whisper a second time. Returns "" if there's nothing usable."""
    transcribe = getattr(gateway, "_transcribe_and_echo_pending_voice", None)
    if transcribe is None:
        return ""
    try:
        _, transcripts = await transcribe(event, adapter, source, event.text or "", log_context="Capture")
    except Exception as exc:
        logger.warning("[discord-routing] Voice transcription for capture failed: %s", exc)
        return ""
    return "\n\n".join(t.strip() for t in transcripts or [] if t and t.strip())


def _channel_lookup(client):
    def lookup(channel_id: int):
        channel = client.get_channel(channel_id) if client is not None else None
        return getattr(channel, "name", None)
    return lookup


async def _run_capture(event, source, adapter, channel_name: str, config: dict, gateway=None) -> str:
    """Save the capture for this message, if any. Returns an extra line for the channel prompt."""
    captures_cfg = config.get("captures") or {}
    kind = (captures_cfg.get("channels") or {}).get(channel_name)
    if not kind or not _allowed_capture_user(source, adapter):
        return ""
    text = event.text or ""
    if text.strip().startswith(("!", "/")):
        return ""
    voice = False
    msg_type = getattr(event.message_type, "value", event.message_type)
    if msg_type in ("voice", "audio") and not captures.clean_text(text):
        transcript = await _voice_transcript(event, source, adapter, gateway)
        if not transcript:
            notice = "🎙️ I couldn't transcribe that voice note, so it wasn't saved. Type it and I'll save it."
            if adapter is not None:
                await adapter.send(source.chat_id, notice)
            return ("CAPTURE: this was a voice note the plugin couldn't transcribe, so nothing was saved. "
                    "Don't write any files; just reply.")
        text, voice = transcript, True
    text = captures.resolve_channel_mentions(text, _channel_lookup(getattr(adapter, "_client", None)))

    writer = captures.CaptureWriter(Path(captures_cfg.get("vault_path", "~/Documents/ProxVault")))
    now = event.timestamp if isinstance(event.timestamp, datetime) else datetime.now()
    now = now.astimezone()
    thread_id = source.thread_id or (source.chat_id if source.chat_type == "thread" else "")
    try:
        if kind == "brain_dump":
            result = writer.brain_dump(text, str(event.message_id or ""), str(thread_id or ""), now, voice=voice)
        elif kind == "daily_goals":
            result = writer.daily_goals(text, now, voice=voice)
        elif kind == "focus_session":
            result = writer.focus_session(text, now)
        else:
            logger.warning("[discord-routing] Unknown capture kind %r for #%s", kind, channel_name)
            return ""
    except Exception as exc:
        logger.warning("[discord-routing] Capture failed in #%s: %s", channel_name, exc)
        return ("CAPTURE FAILED: the plugin could not save this message (" + str(exc)[:200] + "). "
                "Tell Nathan it wasn't saved. Don't try to save it yourself.")

    if result.saved:
        logger.info("[discord-routing] Capture #%s: %s", channel_name, result.summary)
        raw = getattr(event, "raw_message", None)
        if raw is not None and hasattr(raw, "add_reaction"):
            try:
                await raw.add_reaction("📝")
            except Exception as exc:
                logger.debug("[discord-routing] Reaction failed: %s", exc)
    if not result.summary:
        return ("CAPTURE: nothing was saved for this message. Don't write, create or edit any files; "
                "just reply.")
    return (f"CAPTURE DONE BY THE SYSTEM: the routing plugin already {result.summary}. "
            "Do NOT write, create or edit any files and don't call tools to save anything. "
            "Reply in one or two lines and mention the filename if there is one.")


async def _on_pre_gateway_dispatch(event=None, gateway=None, **_kwargs):
    source = getattr(event, "source", None)
    if source is None or gateway is None:
        return None
    platform = getattr(source.platform, "value", source.platform)
    if platform != "discord" or source.chat_type == "dm":
        return None
    config = _load_config()
    if not config:
        return None

    adapter = getattr(gateway, "adapters", {}).get(source.platform)
    client = getattr(adapter, "_client", None)
    channel_name = _channel_name(source, client)
    text = (event.text or "").strip()
    guard.configure(config)
    guard.remember(source.chat_id, channel_name, client)

    if text.lower() == "!model":
        routing = _routing_for(channel_name, config)
        label = f"{routing['label']} [{routing['tier']}]" if routing else "default (no routing config)"
        if adapter is not None:
            await adapter.send(source.chat_id, f"📊 **Channel model** (`#{channel_name}`): {label}")
        return {"action": "skip", "reason": "discord-routing !model"}

    escalated = text.lower().startswith("!opus")
    routing = _opus_for(config) if escalated else _routing_for(channel_name, config)
    if not routing:
        return None

    session_key = gateway._session_key_for_source(source)
    gateway._session_model_overrides[session_key] = {
        "model": routing["model"],
        "provider": routing["provider"],
        "api_key": routing["api_key"],
        "base_url": routing["base_url"],
        "api_mode": routing["api_mode"],
    }
    logger.info(
        "[discord-routing] #%s → model: %s%s",
        channel_name, routing["label"], " (opus escalation)" if escalated else "",
    )
    guard.record("ROUTE", f"#{channel_name}",
                 f"{source.user_name or 'unknown'} ({source.user_id}) → {routing['label']}"
                 + (" [!opus]" if escalated else ""))

    # A prompt in routing.json replaces any ID-keyed discord.channel_prompts entry.
    channel_prompt = config.get("prompts", {}).get(channel_name)
    if channel_prompt:
        event.channel_prompt = channel_prompt

    # Cloud tiers carry vault_blocked_folders; !opus is always cloud, so it uses tier3's list.
    blocked = routing["vault_blocked_folders"] or (
        config.get("tiers", {}).get("tier3", {}).get("vault_blocked_folders", []) if escalated else []
    )
    privacy = _privacy_prompt(blocked)
    if privacy:
        event.channel_prompt = f"{event.channel_prompt}\n\n{privacy}" if event.channel_prompt else privacy

    # Cost: drop the backfilled channel history where each message stands alone.
    if channel_name in (config.get("history_backfill") or {}).get("off", []) and getattr(event, "channel_context", None):
        event.channel_context = None

    # Tell the model which tools this channel allows (the guard enforces it either way).
    tools_line = guard.policy.prompt_line(f"#{channel_name}")
    if tools_line:
        event.channel_prompt = f"{event.channel_prompt}\n\n{tools_line}" if event.channel_prompt else tools_line

    # Capture channels: the plugin saves the note, the model only replies.
    if not escalated:
        capture_prompt = await _run_capture(event, source, adapter, channel_name, config, gateway)
        if capture_prompt:
            event.channel_prompt = (
                f"{event.channel_prompt}\n\n{capture_prompt}" if event.channel_prompt else capture_prompt
            )

    if client is not None and channel_name != _AUDIT_CHANNEL:
        asyncio.create_task(
            _send_audit(client, channel_name, source.user_name or "unknown", routing["label"], escalated)
        )

    if escalated:
        body = text[5:].strip()
        if body:
            return {"action": "rewrite", "text": body}
    return None


# Files Nathan sends are read-only in /root/.hermes/cache/... inside the sandbox (the guard allows
# those folders); files for him go in /output, mounted from ~/.local/share/proxai/outbox.
_FILE_WORDS = re.compile(
    r"\[The user sent (?:a|an) |/root/\.hermes/(?:cache|attachments)/|\b(?:file|files|attach\w*|pdf|"
    r"docx?|csv|xlsx|pptx|spreadsheet|download|export|upload\w*|send (?:me|it|that|back)|"
    r"zip)\b", re.I)
_FILES_HINT = (
    "[files] Files Nathan sends are saved read-only under /root/.hermes/cache/ (documents, images, "
    "audio) in your sandbox: open them with read_file at the exact path given (PDF, Word and "
    "Excel are turned into text for you). To send Nathan a file, save it in /output/ (for "
    "example /output/summary.md) and put MEDIA:/output/summary.md on its own line in your reply. "
    "Files saved anywhere else can't be sent.")


def _on_pre_llm_call(user_message=None, **_kwargs):
    """Point the model at the upload folders and /output, only when the message is about files."""
    text = user_message if isinstance(user_message, str) else json.dumps(user_message or "")[:4000]
    if not _FILE_WORDS.search(text or ""):
        return None
    return {"context": _FILES_HINT}


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    # Tool guard: protected paths blocked, every call logged to the write-only security log.
    guard.configure(_load_config())
    ctx.register_hook("pre_tool_call", guard.pre_tool_call)
    ctx.register_hook("post_tool_call", guard.post_tool_call)
    # Same channel limits, applied to the request: blocked tools aren't sent (saves tokens).
    ctx.register_middleware("llm_request", guard.trim_tools)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_tool(
        name="report_security_event", toolset="proxai_security", schema=REPORT_SCHEMA,
        handler=guard.report_tool, emoji="🚨",
        description="Log a security concern to the write-only 07-Security log",
    )
