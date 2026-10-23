"""Folder rules for vault writes, shared by the plugin (asks Nathan) and apply.py (writes).

Both check every request, so a bug in one half can't write where the rules say no.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
HOWS = ("create", "replace", "append")


class Refused(Exception):
    """A write the rules don't allow; the message is shown to the model/Nathan."""


def vault_dir() -> Path:
    return Path(os.environ.get("PROXAI_VAULT", "~/Documents/ProxVault")).expanduser()


def state_dir() -> Path:
    return Path(os.environ.get("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser()


def load_policy(path: Path | None = None) -> dict:
    p = json.loads((path or HERE / "policy.json").read_text(encoding="utf-8"))
    return {"free": list(p.get("free", [])), "never": list(p.get("never", [])),
            "extensions": [e.lower() for e in p.get("extensions", [".md"])],
            "max_bytes": int(p.get("max_bytes", 200_000))}


def normalise(raw: str) -> str:
    """Vault-relative POSIX path. Accepts '04-Knowledge/x.md', the container path
    '/workspace/vault/04-Knowledge/x.md' or the host path. Refuses '..', '.', absolute paths
    outside the vault and empty names."""
    p = str(raw or "").strip().replace("\\", "/")
    for prefix in ("/workspace/vault/", str(vault_dir()) + "/", "vault/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    if not p or p.startswith("/") or p.startswith("~"):
        raise Refused("Give a path inside the vault, like 04-Knowledge/note.md.")
    parts = PurePosixPath(p).parts
    if any(part in ("..", ".") for part in p.split("/")) or not parts:
        raise Refused("Vault paths can't contain '..' or '.'.")
    return "/".join(parts)


def _under(rel: str, entry: str) -> bool:
    entry = entry.strip("/")
    return rel == entry or rel.startswith(entry + "/")


def rule_for(rel: str, policy: dict) -> str:
    """'never' | 'free' | 'ask'."""
    if any(part.startswith(".") for part in rel.split("/")):
        return "never"
    if any(_under(rel, e) for e in policy["never"]):
        return "never"
    if any(_under(rel, e) for e in policy["free"]):
        return "free"
    return "ask"


def check(path: str, content: str, how: str, policy: dict) -> tuple[str, str]:
    """(relative path, rule) for an allowed write, else Refused. Doesn't look at approval."""
    if how not in HOWS:
        raise Refused(f"how must be one of {', '.join(HOWS)}.")
    rel = normalise(path)
    rule = rule_for(rel, policy)
    if rule == "never":
        raise Refused(f"{rel.split('/')[0]} is off limits; Hermes can't write there.")
    ext = PurePosixPath(rel).suffix.lower()
    if ext not in policy["extensions"]:
        raise Refused(f"Only {', '.join(policy['extensions'])} files can be written to the vault.")
    size = len(str(content or "").encode("utf-8"))
    if size > policy["max_bytes"]:
        raise Refused(f"Content is {size} bytes; the limit is {policy['max_bytes']}.")
    if how != "append" and not str(content or "").strip():
        raise Refused("Content is empty.")
    return rel, rule
