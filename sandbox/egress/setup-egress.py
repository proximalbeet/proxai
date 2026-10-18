#!/usr/bin/env python3
"""Write the ProxAI egress proxy's state: CA, stand-in token and iron-proxy config.

State lives in ~/.hermes/profiles/proxai/egress/ (0700, not in git):
  ca.crt / ca.key        proxy's own certificate authority (only the sandbox trusts it)
  cc-token               stand-in token mounted into the sandbox (useless without the proxy)
  claude-oauth.env       CLAUDE_CODE_OAUTH_TOKEN=<real token from `claude setup-token`>, you create it
  proxy.yaml             iron-proxy config (JSON is valid YAML)

Safe to re-run: keeps the CA and stand-in token, rewrites proxy.yaml from allowed-hosts.txt.
"""
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path.home() / ".hermes/profiles/proxai/egress"
TUNNEL_PORT = 9090
REAL_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_HOST = "api.anthropic.com"
REQUEST_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
# Never let an allowed name resolve to the host, LAN, Tailscale or cloud metadata.
DENY_CIDRS = [
    "127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
    "::ffff:0:0/96", "100.64.0.0/10", "198.18.0.0/15", "0.0.0.0/8",
]


def write_private(path: Path, data: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, mode)


def allowed_hosts() -> list[str]:
    hosts = []
    for line in (HERE / "allowed-hosts.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line not in hosts:
            hosts.append(line)
    if TOKEN_HOST not in hosts:
        sys.exit(f"{TOKEN_HOST} must stay in allowed-hosts.txt (Claude Code needs it)")
    return hosts


def ensure_ca() -> None:
    crt, key = STATE / "ca.crt", STATE / "ca.key"
    if crt.exists() and key.exists():
        return
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-days", "3650", "-subj", "/CN=ProxAI sandbox egress CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    os.chmod(key, 0o600)
    # The proxy runs as nobody inside its container and must read the key; the
    # folder itself stays 0700 so nothing else on the host can.
    os.chmod(crt, 0o644)


def ensure_stand_in_token() -> str:
    path = STATE / "cc-token"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    # Shaped like a real OAuth token so Claude Code accepts it; worthless outside the proxy.
    token = "sk-ant-oat01-proxai-standin-" + secrets.token_urlsafe(32)
    write_private(path, token, 0o644)
    return token


def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    ensure_ca()
    token = ensure_stand_in_token()
    hosts = allowed_hosts()
    config = {
        "dns": {"listen": "127.0.0.1:0", "proxy_ip": "127.0.0.1"},
        "proxy": {
            "tunnel_listen": f"0.0.0.0:{TUNNEL_PORT}",
            "http_listen": f"0.0.0.0:{TUNNEL_PORT + 1}",
            "https_listen": "127.0.0.1:0",
            "max_request_body_bytes": 16 * 1024 * 1024,
            "max_response_body_bytes": 0,
            "upstream_response_header_timeout": "120s",
            "upstream_deny_cidrs": DENY_CIDRS,
        },
        "metrics": {"listen": "127.0.0.1:0"},
        "tls": {"ca_cert": "/etc/iron-proxy/ca.crt", "ca_key": "/etc/iron-proxy/ca.key",
                "cert_cache_size": 1000, "leaf_cert_expiry_hours": 168},
        "transforms": [
            {"name": "allowlist", "config": {"domains": hosts}},
            {"name": "secrets", "config": {"secrets": [{
                "source": {"type": "env", "var": REAL_ENV},
                "replace": {"proxy_value": token, "match_headers": ["Authorization"],
                            "match_query": False, "match_body": False, "require": True},
                # Not CONNECT: the tunnel handshake carries no token, so `require` would refuse
                # every connection. Each request inside the tunnel still needs the stand-in.
                "rules": [{"host": TOKEN_HOST, "methods": REQUEST_METHODS}],
            }]}},
        ],
        "log": {"level": "info"},
    }
    write_private(STATE / "proxy.yaml", json.dumps(config, indent=2) + "\n", 0o644)

    real = STATE / "claude-oauth.env"
    print(f"egress state: {STATE}")
    print(f"allowed: {', '.join(hosts)}")
    if not real.exists():
        print(f"\nMissing {real}. Create it with your subscription token:\n"
              f"  claude setup-token\n"
              f"  printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\\n' '<token>' > {real} && chmod 600 {real}")
    elif stat.S_IMODE(real.stat().st_mode) & 0o077:
        os.chmod(real, 0o600)
        print(f"tightened {real} to 600")


if __name__ == "__main__":
    main()
