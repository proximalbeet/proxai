#!/usr/bin/env python3
"""One-time setup for the ProxAI calendar (checklist Phase 9). Run on the desktop, not by Hermes.

  python3 setup_calendar.py --client-secret ~/Downloads/client_secret_XXXX.json
      copies the OAuth client in, signs in through the browser (scopes: calendar.app.created
      + calendar.freebusy), saves token.json (600) and creates the "ProxAI" calendar
  python3 setup_calendar.py --no-freebusy ...   leave out busy times
  python3 setup_calendar.py --selftest
      create → move → delete a test event on ProxAI, then check that writing to and reading
      the main calendar are refused (the permission test)

Python stdlib only. Files go to ~/.hermes/profiles/proxai/google-calendar/ (700/600, not in git).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (CONFIG_DIR, SCOPE_APP, SCOPE_FREEBUSY, TOKEN_URL, Calendar,  # noqa: E402
                  CalendarError, check_scopes, urllib_http, write_private_json)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def system_tz() -> str:
    try:
        tz = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if tz:
            return tz
    except (OSError, subprocess.SubprocessError):
        pass
    link = Path("/etc/localtime").resolve()
    return "/".join(link.parts[-2:]) if "zoneinfo" in link.parts else "UTC"


def sign_in(client: dict, scopes: list[str]) -> dict:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("ProxAI calendar: signed in, you can close this tab.".encode()
                             if "code" in result else b"Sign-in failed; check the terminal.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "false", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Opening the Google sign-in page. If it doesn't open, paste this into the browser:\n" + url)
    webbrowser.open(url)
    server.timeout = 300
    while not result:
        server.handle_request()
        if not result:
            raise SystemExit("No sign-in response (timed out or state mismatch).")
    if "code" not in result:
        raise SystemExit(f"Sign-in refused: {result.get('error')}")
    status, tok = urllib_http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                              urllib.parse.urlencode({
                                  "code": result["code"], "client_id": client["client_id"],
                                  "client_secret": client["client_secret"], "redirect_uri": redirect,
                                  "grant_type": "authorization_code", "code_verifier": verifier,
                              }).encode())
    if status != 200 or "refresh_token" not in tok:
        raise SystemExit(f"Token exchange failed ({status}): {tok.get('error_description') or tok}")
    check_scopes(tok.get("scope", ""))  # refuses anything broader than allowed
    return {"refresh_token": tok["refresh_token"], "scope": tok.get("scope", ""),
            "created": datetime.now().isoformat(timespec="seconds")}


def setup(args) -> None:
    dest = CONFIG_DIR / "client_secret.json"
    if args.client_secret:
        src = Path(args.client_secret).expanduser()
        data = json.loads(src.read_text())
        if "installed" not in data:
            raise SystemExit("Use an OAuth client of type 'Desktop app' (the JSON should have 'installed').")
        write_private_json(dest, data)
        print(f"client secret saved to {dest} (600). You can delete {src} now.")
    if not dest.exists():
        raise SystemExit("Pass --client-secret <downloaded json> the first time.")
    client = json.loads(dest.read_text())["installed"]
    scopes = [SCOPE_APP] + ([] if args.no_freebusy else [SCOPE_FREEBUSY])
    token = sign_in(client, scopes)
    write_private_json(CONFIG_DIR / "token.json", token)
    print(f"signed in; scopes granted: {token['scope']}")

    cal = Calendar()
    cfg_path = CONFIG_DIR / "calendar.json"
    if cfg_path.exists():
        print(f"calendar already set up: {json.loads(cfg_path.read_text())['calendar_id']}")
        return
    tz = system_tz()
    created = cal.api("POST", "/calendars", body={
        "summary": args.name, "timeZone": tz,
        "description": "Events made by ProxAI (Hermes). It can only see and change this calendar."})
    write_private_json(cfg_path, {"calendar_id": created["id"], "time_zone": tz, "name": args.name})
    print(f"created calendar '{args.name}' ({tz}); it appears in Google Calendar under 'My calendars'.")


def selftest() -> int:
    cal, ok = Calendar(), True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")

    day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    ev = cal.create("ProxAI self-test (safe to delete)", f"{day}T09:00", f"{day}T09:15")
    check("create on ProxAI calendar", bool(ev.get("event_id")), ev.get("start", ""))
    moved = cal.update(ev["event_id"], start=f"{day}T09:30")
    check("move keeps length", moved["start"].startswith(f"{day}T09:30") and moved["end"].startswith(f"{day}T09:45"),
          f"{moved['start']} → {moved['end']}")
    listed = [e["event_id"] for e in cal.list_events(f"{day}T00:00", f"{day}T23:59")]
    check("list shows it", ev["event_id"] in listed)
    cal.delete(ev["event_id"])
    check("delete", ev["event_id"] not in [e["event_id"] for e in cal.list_events(f"{day}T00:00", f"{day}T23:59")])

    try:
        cal.api("POST", "/calendars/primary/events", {"sendUpdates": "none"},
                {"summary": "SHOULD FAIL", "start": {"date": day}, "end": {"date": day}})
        check("write to main calendar refused", False, "it was allowed! delete 'SHOULD FAIL' and re-check scopes")
    except CalendarError as e:
        check("write to main calendar refused", "permission" in str(e) or "not found" in str(e), str(e))
    try:
        cal.api("GET", "/calendars/primary/events", {"maxResults": "1"})
        check("read main calendar refused", False, "events were readable")
    except CalendarError as e:
        check("read main calendar refused", True, str(e))
    try:
        busy = cal.busy()
        check("busy times (no details)", all(set(b) <= {"start", "end"} for b in busy), f"{len(busy)} busy block(s)")
    except CalendarError as e:
        print(f"SKIP  busy times: {e}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-secret", help="OAuth desktop client JSON downloaded from Google Cloud Console")
    p.add_argument("--no-freebusy", action="store_true", help="don't ask for busy times")
    p.add_argument("--name", default="ProxAI", help="calendar name (default ProxAI)")
    p.add_argument("--selftest", action="store_true", help="run the permission test")
    a = p.parse_args()
    try:
        if a.selftest:
            return selftest()
        setup(a)
        return 0
    except CalendarError as e:
        print(f"FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
