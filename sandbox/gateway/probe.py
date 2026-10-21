"""Check the gateway sandbox from the inside. Run by check.sh under the same rules as the gateway.

Never opens or lists private files: hidden folders are checked by counting entries only,
and the vault is checked with os.access (no files created there).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

H = Path.home()
VAULT = H / "Documents/ProxVault"
results = []


def check(ok, what):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def warn(ok, what):
    print(f"{'PASS' if ok else 'WARN'}  {what}")


def visible(p):
    return os.path.exists(p)


def hidden(p):
    """Missing, unreadable, or an empty placeholder."""
    p = Path(p)
    if not p.exists():
        return True
    if p.is_dir():
        try:
            return len(os.listdir(p)) == 0
        except OSError:
            return True
    return not os.access(p, os.R_OK)


def writable(p):
    return os.access(p, os.W_OK)


def write_test(folder):
    """Actually create and delete a temp file (only used outside the vault)."""
    try:
        fd, name = tempfile.mkstemp(prefix=".sandbox-probe-", dir=folder)
        os.close(fd)
        os.unlink(name)
        return True
    except OSError:
        return False


print("== Must be visible and writable")
for p in [H / ".hermes", H / ".hermes/kanban", H / ".hermes/cron", H / ".hermes/installs",
          H / ".hermes/profiles/proxai", H / ".hermes/profiles/proxai/logs",
          H / ".local/state/proxai", H / ".local/state/hermes", H / ".local/share/proxai/outbox"]:
    check(write_test(p), f"write {p}")
for p in [VAULT / "05-Brain-Dump", VAULT / "_inbox", VAULT / "07-Security/ProxAI-Audit"]:
    check(writable(p), f"writable {p}")
for p in [H / ".cache/huggingface"]:
    check(write_test(p), f"write {p}")

print("== Must be visible, read-only")
for p in [H / ".hermes/hermes-agent", H / ".hermes/tools", VAULT / "04-Knowledge", VAULT / "00-Daily",
          H / "dev/proxai"]:
    check(visible(p) and not writable(p), f"read-only {p}")
check((H / ".hermes/profiles/proxai/config.yaml").is_file(), "profile config.yaml readable")
check(os.access(H / ".hermes/profiles/proxai/.env", os.R_OK), "profile .env readable (not printed)")
whisper = H / ".cache/huggingface/hub/models--Systran--faster-whisper-base"
check(whisper.is_dir(), "local Whisper model cache visible")

print("== Must be hidden")
for p in [H / ".ssh", H / ".git-credentials", H / ".config", H / ".config/systemd/user", H / "Downloads",
          H / ".local/share/claude",
          H / "Documents/apikeys.txt", H / ".hermes/profiles/onetouchai", H / ".hermes/profiles/huskytutor",
          VAULT / "01-Personal", VAULT / ".git", VAULT / ".obsidian"]:
    check(hidden(p), f"hidden {p}")
try:
    docs = sorted(os.listdir(H / "Documents"))
except OSError:
    docs = []
check(docs == ["ProxVault"], f"~/Documents shows only ProxVault ({len(docs)} entries)")
try:
    sec = os.listdir(VAULT / "07-Security")
except OSError:
    sec = []
check(sec == ["ProxAI-Audit"], f"07-Security shows only ProxAI-Audit ({len(sec)} entries)")
try:
    home = sorted(os.listdir(H))
except OSError:
    home = []
print(f"      home folder shows: {', '.join(home)}")

print("== Services")
try:
    r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                       capture_output=True, text=True, timeout=20)
    check(r.returncode == 0, f"docker reachable (server {r.stdout.strip() or r.stderr.strip()[:80]})")
except Exception as e:
    check(False, f"docker reachable ({e})")
try:
    with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as resp:
        n = len(json.load(resp).get("models", []))
    check(True, f"Ollama reachable ({n} models)")
except Exception as e:
    check(False, f"Ollama reachable ({e})")
try:
    r = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway-proxai"],
                       capture_output=True, text=True, timeout=10)
    check(r.stdout.strip() in ("active", "inactive", "activating", "failed"),
          f"user systemd reachable (gateway is {r.stdout.strip() or r.stderr.strip()[:80]})")
except Exception as e:
    check(False, f"user systemd reachable ({e})")
try:
    socket.getaddrinfo("discord.com", 443)
    check(True, "DNS works (discord.com)")
except OSError as e:
    check(False, f"DNS works ({e})")
try:
    socket.create_connection(("127.0.0.1", 8653), timeout=2).close()
    warn(True, "Kanye voice server port 8653 (only up in server mode)")
except OSError:
    warn(False, "Kanye voice server port 8653 not answering (normal in personal mode)")

failed = results.count(False)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
