#!/usr/bin/env python3
"""Phase 10: switch StormTrooper between SERVER and PERSONAL mode.

    proxai-mode status                   current mode, when, by whom, last result
    proxai-mode check server             would the switch be allowed right now? (readings)
    proxai-mode set server|personal      switch (server refuses if the PC is busy; --force skips that)
    proxai-mode set personal --dry-run   print what would happen, change nothing
    proxai-mode reapply                  re-run the current mode's steps (at login)
    proxai-mode modes                    list modes and their steps

What each mode changes lives in modes.toml next to this file. The current mode is written to
~/.local/state/proxai/mode.json; Hermes plugins (routing, voice) read it on every message, so
their part takes effect without a gateway restart. Every switch is appended to mode.log.

User-level only: systemctl --user, Ollama's user service, KDE, OpenRGB. No sudo.
Standard library only (Python 3.11+), so Hermes's venv and the system python can both run it.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("PROXAI_MODES", HERE / "modes.toml"))
STATE_DIR = Path(os.getenv("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser()
MODE_FILE = STATE_DIR / "mode.json"
LOG_FILE = STATE_DIR / "mode.log"
LOCK_FILE = STATE_DIR / "mode.lock"
SYSTEMD_USER_DIR = Path(os.getenv("PROXAI_SYSTEMD_USER_DIR", "~/.config/systemd/user")).expanduser()
OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = f"http://{OLLAMA_URL}"
VOICE_URL = os.getenv("PROXAI_VOICE_URL", "http://127.0.0.1:8653")
ENV_FILE = Path(os.getenv("PROXAI_ENV_FILE", "~/.hermes/profiles/proxai/.env")).expanduser()
OPENRGB_DIR = Path(os.getenv("PROXAI_OPENRGB_DIR", "~/.config/OpenRGB")).expanduser()

SLEEP_UNIT = "proxai-mode-sleep-block"
DROPIN_NAME = "50-proxai-mode.conf"
NO_REAPPLY = {"lock_screen", "display"}


class ModeError(Exception):
    pass


# ---- running commands (swapped for a fake in tests) -------------------------------------------

class Runner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.planned: list[str] = []

    def run(self, argv: list[str], timeout: float = 60) -> tuple[int, str]:
        if self.dry_run:
            self.planned.append(" ".join(argv))
            return 0, "(dry run)"
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return 127, f"{argv[0]}: not installed"
        except subprocess.TimeoutExpired:
            return 124, f"{argv[0]}: timed out after {timeout:.0f}s"
        return p.returncode, (p.stdout + p.stderr).strip()

    def http(self, url: str, body: dict | None = None, headers: dict | None = None,
             timeout: float = 120) -> tuple[int, str]:
        if self.dry_run:
            self.planned.append(f"POST {url} {json.dumps(body or {})}")
            return 200, "(dry run)"
        req = urllib.request.Request(url, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode(errors="replace")[:2000]
        except Exception as exc:
            return 0, str(exc)

    def write_file(self, path: Path, text: str) -> bool:
        """Write only if different. Returns True when the file changed."""
        try:
            if path.read_text() == text:
                return False
        except OSError:
            pass
        if self.dry_run:
            self.planned.append(f"write {path}")
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return True

    def remove_file(self, path: Path) -> bool:
        if not path.exists():
            return False
        if self.dry_run:
            self.planned.append(f"remove {path}")
            return True
        path.unlink()
        return True


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: str = ""
    skipped: bool = False


# ---- config + state ---------------------------------------------------------------------------

def load_config(path: Path = None) -> dict:
    path = Path(path or CONFIG_PATH)
    try:
        cfg = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModeError(f"can't read {path}: {exc}")
    modes = {k: v for k, v in cfg.items() if isinstance(v, dict) and "steps" in v}
    if not modes:
        raise ModeError(f"no modes with steps in {path}")
    return modes


def read_state() -> dict:
    try:
        return json.loads(MODE_FILE.read_text())
    except (OSError, ValueError):
        return {"mode": "unknown"}


def current_mode() -> str:
    return read_state().get("mode", "unknown")


def hermes_settings() -> dict:
    """What Hermes plugins should do in the current mode ({} when no mode is set)."""
    return read_state().get("hermes") or {}


def _write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MODE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.chmod(tmp, 0o644)
    tmp.replace(MODE_FILE)


def _log(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---- preflight: is the machine busy? ----------------------------------------------------------

def _gpu_sample(runner: Runner) -> dict | None:
    rc, out = runner.run(["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,memory.used,"
                          "memory.total,temperature.gpu", "--format=csv,noheader,nounits"], timeout=10)
    if rc != 0 or not out or out == "(dry run)":
        return None
    try:
        util, power, used, total, temp = [float(x) for x in out.splitlines()[0].split(",")]
    except ValueError:
        return None
    return {"util": util, "power": power, "vram_used": used, "vram_total": total, "temp": temp}


def _gpu_processes(runner: Runner) -> list[dict]:
    rc, out = runner.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                          "--format=csv,noheader,nounits"], timeout=10)
    procs = []
    if rc != 0:
        return procs
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[2].isdigit():
            procs.append({"pid": parts[0], "name": ",".join(parts[1:-1]), "mib": int(parts[-1])})
    return procs


def _cpu_times() -> tuple[int, int]:
    fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def _cmdlines() -> list[tuple[str, str]]:
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                out.append((d.name, (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")))
            except OSError:
                pass
    return out


def preflight(target: str, modes: dict, runner: Runner = None, sleep=time.sleep,
              cpu_times=_cpu_times, cmdlines=_cmdlines) -> dict:
    """Readings + reasons the switch to `target` should be refused (empty reasons = OK).
    Read-only, so a dry run still takes real readings."""
    runner = Runner() if runner is None or runner.dry_run else runner
    rules = modes.get(target, {}).get("preflight")
    if not rules:
        return {"ok": True, "reasons": [], "readings": {}}
    seconds = max(1, int(rules.get("sample_seconds", 3)))
    samples, (t0, i0) = [], cpu_times()
    for _ in range(seconds):
        if (s := _gpu_sample(runner)):
            samples.append(s)
        sleep(1)
    t1, i1 = cpu_times()
    cpu = 100.0 * (1 - (i1 - i0) / max(1, t1 - t0))
    readings: dict = {"cpu_busy_pct": round(cpu, 1)}
    reasons: list[str] = []
    if samples:
        avg = {k: sum(s[k] for s in samples) / len(samples) for k in samples[0]}
        readings.update({"gpu_util_pct": round(avg["util"], 1), "gpu_power_w": round(avg["power"], 1),
                         "vram_used_mib": int(avg["vram_used"]), "vram_total_mib": int(avg["vram_total"]),
                         "gpu_temp_c": int(avg["temp"])})
        if avg["util"] > rules.get("gpu_util_max_pct", 100):
            reasons.append(f"GPU busy: {avg['util']:.0f}% (limit {rules['gpu_util_max_pct']}%)")
        if avg["power"] > rules.get("gpu_power_max_w", 10_000):
            reasons.append(f"GPU drawing {avg['power']:.0f} W (limit {rules['gpu_power_max_w']} W)")
    else:
        readings["gpu"] = "nvidia-smi unavailable"
    if cpu > rules.get("cpu_busy_max_pct", 100):
        reasons.append(f"CPU busy: {cpu:.0f}% (limit {rules['cpu_busy_max_pct']}%)")

    ignore = [s.lower() for s in rules.get("ignore_processes", [])]
    limit = rules.get("foreign_vram_max_mib", 10**9)
    heavy = [p for p in _gpu_processes(runner)
             if p["mib"] > limit and not any(i in p["name"].lower() for i in ignore)]
    for p in heavy:
        reasons.append(f"{Path(p['name'].split()[0]).name} is using {p['mib']} MiB of VRAM")
    readings["gpu_processes"] = [f"{Path(p['name'].split()[0]).name}: {p['mib']} MiB"
                                 for p in _gpu_processes(runner)]

    # Whole words of the command line only ("obs" must not match "obsidian"), a ".exe" suffix allowed.
    patterns = {p.lower(): p for p in rules.get("block_cmdline", [])}
    me = str(os.getpid())
    seen = set()
    for pid, cmd in cmdlines():
        if pid == me or "proxai_mode" in cmd:
            continue
        for token in re.split(r"[\s/\\=]+", cmd.lower()):
            name = token[:-4] if token.endswith(".exe") else token
            if name in patterns and name not in seen:
                seen.add(name)
                reasons.append(f"'{patterns[name]}' is running ({cmd.strip()[:80]})")
    return {"ok": not reasons, "reasons": reasons, "readings": readings}


# ---- steps ------------------------------------------------------------------------------------

def _units(runner: Runner, verb: str, units: list[str]) -> StepResult:
    if not units:
        return StepResult(f"{verb}_units", True, "nothing to do", skipped=True)
    rc, out = runner.run(["systemctl", "--user", verb, *units])
    return StepResult(f"{verb}_units", rc == 0, f"{verb} {', '.join(units)}" + (f": {out}" if rc else ""))


def _dropin(runner: Runner, unit: str, env: dict) -> bool:
    path = SYSTEMD_USER_DIR / f"{unit}.d" / DROPIN_NAME
    if not env:
        return runner.remove_file(path)
    body = "# Written by proxai-mode (Phase 10). Changes with the mode; don't edit.\n[Service]\n" + \
        "".join(f"Environment={k}={v}\n" for k, v in env.items())
    return runner.write_file(path, body)


def step_ollama(runner: Runner, step: dict) -> StepResult:
    env = {}
    if "keep_alive" in step:
        env["OLLAMA_KEEP_ALIVE"] = str(step["keep_alive"])
    if "max_loaded_models" in step:
        env["OLLAMA_MAX_LOADED_MODELS"] = str(step["max_loaded_models"])
    notes = []
    if _dropin(runner, "ollama.service", env):
        runner.run(["systemctl", "--user", "daemon-reload"])
        rc, out = runner.run(["systemctl", "--user", "restart", "ollama.service"])
        if rc:
            return StepResult("ollama", False, f"restart failed: {out}")
        notes.append("restarted with " + ", ".join(f"{k}={v}" for k, v in env.items()))
        _wait_ollama(runner)
    elif step.get("unload_all"):   # (a restart already unloaded everything)
        for name in _loaded_models(runner):
            runner.http(f"{OLLAMA_URL}/api/generate", {"model": name, "keep_alive": 0}, timeout=30)
            notes.append(f"unloaded {name}")
    ok = True
    for model in step.get("preload", []):
        status, out = runner.http(f"{OLLAMA_URL}/api/generate",
                                  {"model": model, "prompt": "", "keep_alive": step.get("keep_alive", "5m")},
                                  timeout=180)
        if status == 200:
            notes.append(f"loaded {model}")
        else:
            ok = False
            notes.append(f"couldn't load {model}: {out[:200]}")
    return StepResult("ollama", ok, "; ".join(notes) or "settings unchanged")


def _loaded_models(runner: Runner) -> list[str]:
    if runner.dry_run:
        return []
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=5) as resp:
            return [m["name"] for m in json.load(resp).get("models", [])]
    except Exception:
        return []


def _wait_ollama(runner: Runner, seconds: float = 20) -> None:
    if runner.dry_run:
        return
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=2).close()
            return
        except Exception:
            time.sleep(0.5)


def _api_key() -> str:
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("API_SERVER_KEY="):
                v = line.split("=", 1)[1].strip()
                if v[:1] in "\"'" and v[-1:] == v[:1]:
                    return v[1:-1]
                return re.split(r"\s+#", v, maxsplit=1)[0].strip()
    except OSError:
        pass
    return ""


def step_voice_server(runner: Runner, step: dict) -> StepResult:
    svc, sock = "proxai-phone-voice.service", "proxai-phone-voice.socket"
    if not step.get("enabled", True):
        _dropin(runner, svc, {})
        rc, out = runner.run(["systemctl", "--user", "stop", sock, svc])
        return StepResult("voice_server", rc == 0, "voice server off (phone uses its own voice)"
                          + (f": {out}" if rc else ""))
    env = {"PROXAI_VOICE_IDLE": str(int(step["idle_seconds"]))} if "idle_seconds" in step else {}
    changed = _dropin(runner, svc, env)
    notes = []
    if changed:
        runner.run(["systemctl", "--user", "daemon-reload"])
        runner.run(["systemctl", "--user", "stop", svc])   # next start picks up the new idle time
        notes.append(f"idle exit {env.get('PROXAI_VOICE_IDLE', 'default')} s")
    rc, out = runner.run(["systemctl", "--user", "start", sock])
    if rc:
        return StepResult("voice_server", False, f"socket start failed: {out}")
    if step.get("warm"):
        key = _api_key()
        status, out = runner.http(f"{VOICE_URL}/v1/warmup", {"voice": "kanye"},
                                  {"Authorization": f"Bearer {key}"}, timeout=30)
        notes.append("warming up" if status in (200, 202) else f"warmup failed: {out[:120]}")
    return StepResult("voice_server", True, "voice server on" + (f" ({'; '.join(notes)})" if notes else ""))


def step_sleep_block(runner: Runner, step: dict) -> StepResult:
    if step.get("on"):
        rc, _ = runner.run(["systemctl", "--user", "is-active", "--quiet", f"{SLEEP_UNIT}.service"])
        if rc == 0 and not runner.dry_run:
            return StepResult("sleep_block", True, "already blocking sleep")
        rc, out = runner.run(["systemd-run", "--user", f"--unit={SLEEP_UNIT}", "--collect", "--quiet",
                              "systemd-inhibit", "--what=sleep:idle:handle-suspend-key",
                              "--who=ProxAI", "--why=Server mode", "--mode=block", "sleep", "infinity"])
        return StepResult("sleep_block", rc == 0, "sleep blocked" + (f": {out}" if rc else ""))
    rc, out = runner.run(["systemctl", "--user", "stop", f"{SLEEP_UNIT}.service"])
    return StepResult("sleep_block", True, "sleep allowed")


def _graphical_session(runner: Runner) -> str:
    rc, out = runner.run(["loginctl", "list-sessions", "--no-legend"])
    user = os.getenv("USER", "")
    for line in out.splitlines() if rc == 0 else []:
        cols = line.split()
        # SESSION UID USER SEAT LEADER CLASS TTY IDLE SINCE
        if len(cols) >= 4 and cols[2] == user and cols[3].startswith("seat"):
            return cols[0]
    return os.getenv("XDG_SESSION_ID", "")


def step_lock_screen(runner: Runner, step: dict) -> StepResult:
    sid = "SESSION" if runner.dry_run else _graphical_session(runner)
    if not sid:
        return StepResult("lock_screen", False, "no desktop session found")
    rc, out = runner.run(["loginctl", "lock-session", sid])
    return StepResult("lock_screen", rc == 0, "screen locked" + (f": {out}" if rc else ""))


def step_display(runner: Runner, step: dict) -> StepResult:
    state = step.get("state", "on")
    if not shutil.which("kscreen-doctor") and not runner.dry_run:
        return StepResult("display", True, "kscreen-doctor not installed", skipped=True)
    rc, out = runner.run(["kscreen-doctor", "--dpms", "off" if state == "off" else "on"], timeout=15)
    return StepResult("display", rc == 0, f"display {state}" + (f": {out}" if rc else ""))


def step_rgb(runner: Runner, step: dict) -> StepResult:
    profile = str(step.get("profile", "")).strip()
    if not shutil.which("openrgb") and not runner.dry_run:
        return StepResult("rgb", True, "OpenRGB not installed (sudo pacman -S openrgb)", skipped=True)
    if (OPENRGB_DIR / f"{profile}.orp").exists() or runner.dry_run and profile != "off":
        argv = ["openrgb", "--noautoconnect", "--profile", profile]
    elif profile == "off":
        argv = ["openrgb", "--noautoconnect", "--mode", "static", "--color", "000000"]
    else:
        return StepResult("rgb", True, f"no OpenRGB profile named '{profile}' yet "
                          f"(save one in OpenRGB as {OPENRGB_DIR}/{profile}.orp)", skipped=True)
    rc, out = runner.run(argv, timeout=60)
    return StepResult("rgb", rc == 0, f"lights: {profile}" + (f": {out[-200:]}" if rc else ""))


def step_command(runner: Runner, step: dict) -> StepResult:
    argv = step.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return StepResult("command", False, "command step needs argv = [\"program\", \"arg\", ...]")
    rc, out = runner.run([os.path.expanduser(a) for a in argv], timeout=float(step.get("timeout", 60)))
    return StepResult(step.get("name", f"command {argv[0]}"), rc == 0, out[-300:])


STEPS = {
    "stop_units": lambda r, s: _units(r, "stop", s.get("units", [])),
    "start_units": lambda r, s: _units(r, "start", s.get("units", [])),
    "ollama": step_ollama,
    "voice_server": step_voice_server,
    "sleep_block": step_sleep_block,
    "lock_screen": step_lock_screen,
    "display": step_display,
    "rgb": step_rgb,
    "command": step_command,
}


# ---- switching --------------------------------------------------------------------------------

class _Lock:
    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            raise ModeError("another mode switch is running")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def run_steps(mode: str, modes: dict, runner: Runner, reapply: bool = False) -> list[StepResult]:
    results = []
    for step in modes[mode].get("steps", []):
        kind = step.get("type", "")
        if reapply and not step.get("reapply", kind not in NO_REAPPLY):
            continue
        fn = STEPS.get(kind)
        if fn is None:
            results.append(StepResult(kind or "?", False, f"unknown step type '{kind}'"))
            continue
        try:
            results.append(fn(runner, step))
        except Exception as exc:  # one broken step must not stop the rest
            results.append(StepResult(kind, False, f"error: {exc}"))
    return results


def switch(target: str, by: str = "cli", force: bool = False, dry_run: bool = False,
           reapply: bool = False, modes: dict = None, runner: Runner = None) -> dict:
    modes = modes or load_config()
    if target not in modes:
        raise ModeError(f"unknown mode '{target}' (have: {', '.join(modes)})")
    runner = runner or Runner(dry_run=dry_run)
    with _Lock():
        check = {"ok": True, "reasons": [], "readings": {}}
        if not reapply:
            check = preflight(target, modes, runner)
            if not check["ok"] and not force:
                result = {"ok": False, "refused": True, "mode": current_mode(), "target": target,
                          "by": by, "reasons": check["reasons"], "readings": check["readings"],
                          "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
                if not dry_run:
                    _log(result)
                return result
        steps = run_steps(target, modes, runner, reapply=reapply)
        state = {
            "mode": target,
            "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "by": by,
            "ok": all(s.ok for s in steps),
            "forced": bool(force and not check["ok"]),
            "description": modes[target].get("description", ""),
            "hermes": modes[target].get("hermes", {}),
            "steps": [asdict(s) for s in steps],
            "preflight": check,
        }
        if reapply:
            state["since"] = read_state().get("since", state["since"])
            state["reapplied"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if dry_run:
            state["dry_run"] = True
            state["planned"] = runner.planned
            return state
        _write_state(state)
        _log({k: v for k, v in state.items() if k != "description"})
        return state


# ---- CLI --------------------------------------------------------------------------------------

def _print_result(r: dict) -> None:
    if r.get("refused"):
        print(f"❌ Refused to switch to {r['target']} mode: the PC is busy.")
        for reason in r["reasons"]:
            print(f"   • {reason}")
        print("   (proxai-mode set server --force to switch anyway)")
        return
    head = "DRY RUN: would switch" if r.get("dry_run") else ("✅ Switched" if r["ok"] else "⚠️  Switched with errors")
    print(f"{head} to {r['mode'].upper()} mode")
    for s in r["steps"]:
        mark = "–" if s["skipped"] else ("✓" if s["ok"] else "✗")
        print(f"   {mark} {s['step']}: {s['detail']}")
    for line in r.get("planned", []):
        print(f"     $ {line}")


def main(argv: list[str] = None) -> int:
    ap = argparse.ArgumentParser(prog="proxai-mode", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("modes")
    sub.add_parser("reapply")
    c = sub.add_parser("check")
    c.add_argument("mode")
    s = sub.add_parser("set")
    s.add_argument("mode")
    s.add_argument("--force", action="store_true", help="switch even if the PC is busy")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--by", default=f"cli:{os.getenv('USER', '?')}")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "status":
            st = read_state()
            if args.json:
                print(json.dumps(st, indent=2))
            else:
                print(f"Mode: {st.get('mode', 'unknown').upper()}"
                      + (f" since {st['since']} (by {st.get('by', '?')})" if st.get("since") else ""))
                bad = [x for x in st.get("steps", []) if not x["ok"]]
                for x in bad:
                    print(f"   ✗ {x['step']}: {x['detail']}")
            return 0
        if args.cmd == "modes":
            for name, m in load_config().items():
                print(f"{name}: {m.get('description', '')}")
                for st in m["steps"]:
                    print(f"   - {st.get('type')}: " + ", ".join(f"{k}={v}" for k, v in st.items() if k != "type"))
            return 0
        if args.cmd == "check":
            r = preflight(args.mode, load_config())
            print(json.dumps(r, indent=2) if args.json else
                  ("✅ OK to switch" if r["ok"] else "❌ Would refuse:\n   • " + "\n   • ".join(r["reasons"]))
                  + "\n" + json.dumps(r["readings"]))
            return 0 if r["ok"] else 2
        if args.cmd == "reapply":
            mode = current_mode()
            if mode == "unknown":
                print("No mode set yet; nothing to reapply.")
                return 0
            r = switch(mode, by="reapply", reapply=True)
        else:
            r = switch(args.mode, by=args.by, force=args.force, dry_run=args.dry_run)
        print(json.dumps(r, indent=2)) if args.json else _print_result(r)
        return 0 if r.get("ok") else (2 if r.get("refused") else 1)
    except ModeError as exc:
        print(f"proxai-mode: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
