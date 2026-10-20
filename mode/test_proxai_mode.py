"""Tests for the Phase 10 mode switch. Nothing on the machine is touched: commands go to a fake
runner and state goes to a temp folder.

Run: python3 mode/test_proxai_mode.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="mode-test-", dir=os.environ.get("TMPDIR")))
os.environ["PROXAI_STATE_DIR"] = str(TMP / "state")
os.environ["PROXAI_SYSTEMD_USER_DIR"] = str(TMP / "systemd")
os.environ["PROXAI_OPENRGB_PORT"] = "1"                    # nothing listens there: no real server
os.environ["PROXAI_ENV_FILE"] = str(TMP / ".env")
(TMP / ".env").write_text('API_SERVER_KEY="k3y"\n')

spec = importlib.util.spec_from_file_location("proxai_mode", HERE / "proxai_mode.py")
pm = importlib.util.module_from_spec(spec)
sys.modules["proxai_mode"] = pm
spec.loader.exec_module(pm)


class FakeRunner(pm.Runner):
    """Records commands; answers nvidia-smi with canned readings."""

    def __init__(self, gpu="10, 30.0, 1000, 16303, 40", apps="", fail=(), rgb=()):
        super().__init__(dry_run=False)
        self.gpu, self.apps, self.fail = gpu, apps, fail
        self.rgb = None if rgb is None else list(rgb)  # OpenRGB server's profiles; None = down
        self.cmds, self.posts, self.files = [], [], {}

    def run(self, argv, timeout=60):
        cmd = " ".join(argv)
        self.cmds.append(cmd)
        if argv[0] == "nvidia-smi":
            return 0, self.gpu if "query-gpu" in cmd else self.apps
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "inactive"
        if any(f in cmd for f in self.fail):
            return 1, "boom"
        if argv[:2] == ["loginctl", "list-sessions"]:
            return 0, f"8 1000 {os.getenv('USER', 'prox')} seat0 115674 user tty2 no -"
        return 0, ""

    def rgb_profiles(self):
        return self.rgb

    def rgb_load(self, name):
        self.cmds.append(f"openrgb load {name}")
        return True, ""

    def http(self, url, body=None, headers=None, timeout=120):
        self.posts.append((url, body, headers))
        # Like real Ollama: a keep_alive string needs a unit ("5m"); "-1" is HTTP 400 (bug of 10-20)
        ka = (body or {}).get("keep_alive")
        if url.endswith("/api/generate") and isinstance(ka, str) and not re.search(r"[a-z]", ka):
            return 400, f'HTTP 400: {{"error":"time: missing unit in duration \\"{ka}\\""}}'
        return 200, "{}"

    def write_file(self, path, text):
        changed = self.files.get(str(path)) != text
        self.files[str(path)] = text
        return changed

    def remove_file(self, path):
        return self.files.pop(str(path), None) is not None


def no_cpu():
    no_cpu.n = getattr(no_cpu, "n", 0) + 1000
    return no_cpu.n, no_cpu.n - 10      # ~1% busy


def busy_cpu():
    busy_cpu.n = getattr(busy_cpu, "n", 0) + 1000
    return busy_cpu.n, busy_cpu.n * 0.1  # ~90% busy


class Base(unittest.TestCase):
    def setUp(self):
        self.modes = pm.load_config(HERE / "modes.toml")
        for f in (pm.MODE_FILE, pm.LOG_FILE):
            f.unlink(missing_ok=True)
        self._orig = (pm._cpu_times, pm._cmdlines)
        pm._cpu_times, pm._cmdlines = no_cpu, lambda: []
        pm.shutil.which = lambda name: f"/usr/bin/{name}"
        pm.time.sleep = lambda s: None

    def tearDown(self):
        pm._cpu_times, pm._cmdlines = self._orig

    def switch(self, mode, runner=None, **kw):
        runner = runner or FakeRunner()
        # preflight's defaults are bound at def time; pass the fakes explicitly
        orig = pm.preflight
        pm.preflight = lambda t, m, r=None: orig(t, m, r, sleep=lambda s: None,
                                                  cpu_times=pm._cpu_times, cmdlines=pm._cmdlines)
        try:
            return pm.switch(mode, modes=self.modes, runner=runner, **kw), runner
        finally:
            pm.preflight = orig


class TestConfig(Base):
    def test_both_modes_load(self):
        self.assertEqual(set(self.modes), {"server", "personal"})
        for m in self.modes.values():
            for step in m["steps"]:
                self.assertIn(step["type"], pm.STEPS)

    def test_unknown_mode(self):
        with self.assertRaises(pm.ModeError):
            self.switch("party")


class TestServer(Base):
    def test_switch_runs_every_step_and_writes_state(self):
        r, run = self.switch("server", FakeRunner(rgb=["Off", "Winterscape"]), by="test")
        self.assertTrue(r["ok"], r["steps"])
        cmds = "\n".join(run.cmds)
        self.assertIn("systemctl --user stop verity-relay.service", cmds)
        self.assertIn("systemctl --user restart ollama.service", cmds)
        self.assertIn("--what=sleep:idle:handle-suspend-key", cmds)
        self.assertIn("loginctl lock-session 8", cmds)
        self.assertIn("kscreen-doctor --dpms off", cmds)
        self.assertIn("openrgb load Off", cmds)                     # "off" matches "Off"
        dropin = run.files[str(pm.SYSTEMD_USER_DIR / "ollama.service.d" / pm.DROPIN_NAME)]
        self.assertIn("OLLAMA_KEEP_ALIVE=-1", dropin)
        self.assertIn(f"OLLAMA_GPU_OVERHEAD={4096 * 1024 * 1024}", dropin)   # room for the voice
        loads = [b for u, b, _ in run.posts if u.endswith("/api/generate")]
        self.assertEqual(loads[0]["model"], self.modes["server"]["hermes"]["tier1"]["model"])
        self.assertEqual(loads[0]["keep_alive"], -1)                 # a number, not the string "-1"
        ollama = [s for s in r["steps"] if s["step"] == "ollama"][0]
        self.assertIn("loaded qwen3.5:35b-a3b", ollama["detail"])
        urls = [u for u, *_ in run.posts]
        self.assertIn(f"{pm.OLLAMA_URL}/api/generate", urls)
        warm = [p for p in run.posts if p[0].endswith("/v1/warmup")][0]
        self.assertEqual(warm[2]["Authorization"], "Bearer k3y")
        st = json.loads(pm.MODE_FILE.read_text())
        self.assertEqual(st["mode"], "server")
        self.assertTrue(st["hermes"]["voice"])
        self.assertIn("tier1", st["hermes"])
        self.assertEqual(json.loads(pm.LOG_FILE.read_text().splitlines()[-1])["mode"], "server")

    def test_refuses_when_gpu_busy(self):
        r, run = self.switch("server", FakeRunner(gpu="95, 280.0, 14000, 16303, 75"))
        self.assertTrue(r["refused"])
        self.assertTrue(any("GPU busy" in x for x in r["reasons"]))
        self.assertTrue(any("drawing" in x for x in r["reasons"]))
        self.assertFalse(pm.MODE_FILE.exists())
        self.assertFalse(any("systemctl" in c for c in run.cmds))   # nothing changed
        self.assertTrue(json.loads(pm.LOG_FILE.read_text().splitlines()[-1])["refused"])

    def test_refuses_when_cpu_busy(self):
        pm._cpu_times = busy_cpu
        r, _ = self.switch("server")
        self.assertTrue(r["refused"])
        self.assertTrue(any("CPU busy" in x for x in r["reasons"]))

    def test_refuses_on_game_vram_but_ignores_our_processes(self):
        apps = ("1250, /home/prox/VERITYexe/verity-voice/.venv/bin/python, 2314\n"
                "77, /usr/bin/ollama, 7000\n"
                "999, Z:\\games\\eldenring.exe, 9000")
        r, _ = self.switch("server", FakeRunner(apps=apps))
        self.assertTrue(r["refused"])
        self.assertEqual(len(r["reasons"]), 1)
        self.assertIn("eldenring.exe", r["reasons"][0])

    def test_refuses_on_steam_game_but_not_obsidian(self):
        pm._cmdlines = lambda: [("1", "/usr/bin/obsidian --enable-features"),
                                ("2", "/usr/lib/systemd/systemd-resolved"),
                                ("3", "steam")]
        r, _ = self.switch("server")
        self.assertFalse(r.get("refused"), r.get("reasons"))
        pm._cmdlines = lambda: [("4", "/home/prox/.steam/ubuntu12_32/reaper SteamLaunch AppId=1245620 -- proton")]
        r, _ = self.switch("server")
        self.assertTrue(r["refused"])

    def test_force_switches_anyway(self):
        r, _ = self.switch("server", FakeRunner(gpu="95, 280.0, 14000, 16303, 75"), force=True)
        self.assertEqual(r["mode"], "server")
        self.assertTrue(r["forced"])

    def test_failed_step_does_not_stop_the_rest(self):
        r, run = self.switch("server", FakeRunner(fail=("verity",)))
        self.assertFalse(r["ok"])
        self.assertEqual(r["mode"], "server")
        self.assertIn("kscreen-doctor --dpms off", "\n".join(run.cmds))

    def test_dry_run_changes_nothing(self):
        r, run = self.switch("server", pm.Runner(dry_run=True), dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertTrue(r["planned"])
        self.assertFalse(pm.MODE_FILE.exists())
        self.assertFalse((TMP / "systemd").exists())


class TestPersonal(Base):
    def test_personal_frees_gpu_and_goes_text_only(self):
        r, run = self.switch("personal")
        self.assertTrue(r["ok"], r["steps"])
        cmds = "\n".join(run.cmds)
        self.assertIn("systemctl --user stop proxai-phone-voice.socket proxai-phone-voice.service", cmds)
        self.assertIn("systemctl --user stop proxai-mode-sleep-block.service", cmds)
        self.assertIn("kscreen-doctor --dpms on", cmds)
        self.assertNotIn("nvidia-smi", cmds)                        # no busy check for personal
        dropin = run.files[str(pm.SYSTEMD_USER_DIR / "ollama.service.d" / pm.DROPIN_NAME)]
        self.assertIn("OLLAMA_KEEP_ALIVE=2m", dropin)
        self.assertNotIn("OLLAMA_GPU_OVERHEAD", dropin)   # personal mode gives games the whole GPU
        self.assertFalse(json.loads(pm.MODE_FILE.read_text())["hermes"]["voice"])

    def test_missing_rgb_profile_is_skipped_not_failed(self):
        r, _ = self.switch("personal")
        rgb = [s for s in r["steps"] if s["step"] == "rgb"][0]
        self.assertTrue(rgb["skipped"] and rgb["ok"])
        self.assertIn("Profile → Save as", rgb["detail"])

    def test_saved_rgb_profile_is_loaded(self):
        prof = self.modes["personal"]["steps"]
        name = [st for st in prof if st["type"] == "rgb"][0]["profile"]
        _, run = self.switch("personal", FakeRunner(rgb=[name]))
        self.assertIn(f"openrgb load {name}", "\n".join(run.cmds))

    def test_rgb_starts_openrgb_server_when_down(self):
        unit = pm.SYSTEMD_USER_DIR / pm.OPENRGB_UNIT
        made_dir = not unit.parent.exists()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("x")
        try:
            r, run = self.switch("server", FakeRunner(rgb=None))
        finally:
            unit.unlink()
            if made_dir:
                unit.parent.rmdir()
        self.assertIn(f"systemctl --user start {pm.OPENRGB_UNIT}", "\n".join(run.cmds))
        rgb = [s for s in r["steps"] if s["step"] == "rgb"][0]
        self.assertFalse(rgb["ok"])                                 # fake start never comes up
        self.assertIn("server isn't running", rgb["detail"])

    def test_reapply_skips_lock_and_display(self):
        self.switch("server")
        r, run = self.switch("server", reapply=True)
        cmds = "\n".join(run.cmds)
        self.assertNotIn("lock-session", cmds)
        self.assertNotIn("kscreen-doctor", cmds)
        self.assertNotIn("nvidia-smi", cmds)                        # no busy check on reapply
        self.assertIn("reapplied", r)


class TestOpenRGBLoad(unittest.TestCase):
    def test_profile_name_sent_without_nul(self):
        sent = []

        class FakeSock:
            def sendall(self, b): sent.append(b)
            def __enter__(self): return self
            def __exit__(self, *a): pass

        orig = pm._orgb_connect
        pm._orgb_connect = lambda: FakeSock()
        try:
            pm.openrgb_load("default", settle=0)
        finally:
            pm._orgb_connect = orig
        packet = sent[-1]
        self.assertEqual(packet[:4], b"ORGB")
        self.assertEqual(pm.struct.unpack("<III", packet[4:16])[1:], (pm.ORGB_LOAD_PROFILE, 7))
        self.assertEqual(packet[16:], b"default")          # "default\0" silently matched nothing (10-20)


class TestKeepAlive(unittest.TestCase):
    def test_api_keep_alive(self):
        self.assertEqual(pm.api_keep_alive("-1"), -1)
        self.assertEqual(pm.api_keep_alive("0"), 0)
        self.assertEqual(pm.api_keep_alive("2m"), "2m")
        self.assertEqual(pm.api_keep_alive(-1), -1)


class TestLock(Base):
    def test_second_switch_while_running_is_refused(self):
        with pm._Lock():
            with self.assertRaises(pm.ModeError):
                self.switch("personal")


if __name__ == "__main__":
    unittest.main(verbosity=1)
