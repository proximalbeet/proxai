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
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

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
    # Fallback: group chat names look like "Guild / #channel".
    name = source.chat_name or ""
    return name.rsplit("#", 1)[-1].strip() if "#" in name else name


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

    if client is not None and channel_name != _AUDIT_CHANNEL:
        asyncio.create_task(
            _send_audit(client, channel_name, source.user_name or "unknown", routing["label"], escalated)
        )

    if escalated:
        body = text[5:].strip()
        if body:
            return {"action": "rewrite", "text": body}
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
