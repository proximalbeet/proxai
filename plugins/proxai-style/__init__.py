"""
How ProxAI answers, enforced per message (the local model often skips long system prompts).

* "How do I / how to / how can I ..." -> explain briefly and stop; don't run tools or do it
  unless Nathan asks (10-23: it went and did things instead of explaining).

Everywhere (Discord, phone, CLI); it only adds a short note to the message.
"""
from __future__ import annotations

import re

_HOW_TO = re.compile(
    r"^\s*(hey\s+)?(proxai[,:]?\s+)?(so\s+|quick question[,:]?\s+|question[,:]?\s+)?"
    r"(how (do|would|can|should|could) (i|we|you)|how to|how does one|what('s| is) the (best )?way to|"
    r"what do i (need to )?do to|walk me through|explain how)\b", re.I)
_DO_IT = re.compile(r"\b(and (then )?(do|run|set|make|fix|install|build) it|do it for me|go ahead and|can you do)\b", re.I)


def _plain(user_message) -> str:
    if isinstance(user_message, list):
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    return re.sub(r"\[[^\]]*\]", " ", str(user_message or "")).strip().strip("\"“”")


def is_how_to(text: str) -> bool:
    return bool(_HOW_TO.search(text)) and not _DO_IT.search(text)


def _on_pre_llm_call(user_message=None, parent_session_id=None, **_kwargs):
    if parent_session_id:
        return None
    if is_how_to(_plain(user_message)):
        return {"context": "[style] Nathan is asking HOW to do something. Explain it briefly (a few lines, "
                           "commands in a code block) and stop. Don't run tools or do it yourself; offer to "
                           "in one short line if useful."}
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
