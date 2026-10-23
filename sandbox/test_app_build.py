"""Tests for proxai-app-build, run in the real sandbox image (no network, as the host user).

Builds sandbox/apps-example/proxai-test in a throwaway workspace and checks the APK, the git
history, rollback and the refusals. Needs Docker and the image from sandbox/build-image.sh.

Run: python3 sandbox/test_app_build.py            (IMAGE=proxai-sandbox:2.1.302-apps by default)
     KEEP=1 python3 sandbox/test_app_build.py     keeps the workspace and prints the APK path
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE = os.environ.get("IMAGE", "proxai-sandbox:2.1.302-apps")
WS = Path(tempfile.mkdtemp(prefix="app-build-test-", dir=os.environ.get("TMPDIR")))
APPS = WS / "apps"
UID = f"{os.getuid()}:{os.getgid()}"


def sh(cmd: str, check=False) -> subprocess.CompletedProcess:
    """Run a shell command inside the sandbox image, workspace mounted like the live container."""
    p = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", UID, "-e", "HOME=/tmp",
                        "-v", f"{WS}:/workspace/proxai", "-w", "/workspace/proxai/apps", IMAGE,
                        "bash", "-c", cmd], capture_output=True, text=True, timeout=300)
    if check and p.returncode != 0:
        raise AssertionError(p.stdout + p.stderr)
    return p


def build(app="proxai-test", extra="") -> dict:
    p = sh(f"proxai-app-build {extra} /workspace/proxai/apps/{app}")
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AssertionError(p.stdout + p.stderr)


def copy_example(name="proxai-test") -> Path:
    dst = APPS / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(HERE / "apps-example" / "proxai-test", dst)
    return dst


class AppBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        APPS.mkdir(parents=True, exist_ok=True)

    def test_1_build_install_ready_apk(self):
        app = copy_example()
        r = build()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["code"], 1)
        self.assertEqual(r["package"], "com.proxai.apps.proxai_test")
        self.assertEqual(r["permissions"], ["android.permission.POST_NOTIFICATIONS", "android.permission.VIBRATE"])
        apk = app / r["apk"]
        self.assertTrue(apk.is_file())
        badging = sh(f"aapt2 dump badging apps/proxai-test/{r['apk']}".replace("apps/", "/workspace/proxai/apps/"),
                     check=True).stdout
        self.assertIn("package: name='com.proxai.apps.proxai_test' versionCode='1' versionName='1.0'", badging)
        self.assertIn("application-label:'ProxAI Test App'", badging)
        self.assertIn("launchable-activity: name='com.proxai.shell.ShellActivity'", badging)
        self.assertIn("minSdkVersion:'29'", badging)
        self.assertNotIn("android.permission.INTERNET", badging)
        files = sh(f"python3 -m zipfile -l /workspace/proxai/apps/proxai-test/{r['apk']}", check=True).stdout
        for f in ("classes.dex", "assets/www/index.html", "assets/proxai-app.json", "res/"):
            self.assertIn(f, files)
        sh(f"apksigner verify /workspace/proxai/apps/proxai-test/{r['apk']}", check=True)
        st = json.loads((app / "status.json").read_text())
        self.assertEqual(st["state"], "built")
        self.assertEqual(st["last_build"]["code"], 1)
        self.assertTrue(st["last_build"]["cert_sha256"])
        self.assertTrue((APPS / ".signing" / "proxai-apps.p12").exists())
        if os.environ.get("KEEP"):
            print(f"\n  APK: {apk}")

    def test_2_update_keeps_history_and_same_key(self):
        app = APPS / "proxai-test"
        first = json.loads((app / "status.json").read_text())["last_build"]
        html = app / "www" / "index.html"
        html.write_text(html.read_text().replace("<h1>ProxAI Test App</h1>", "<h1>ProxAI Test App v2</h1>"))
        meta = json.loads((app / "proxai-app.json").read_text())
        meta["version"] = "1.1"
        (app / "proxai-app.json").write_text(json.dumps(meta))
        r = build()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["code"], 2)
        self.assertEqual(r["cert_sha256"], first["cert_sha256"])      # updates install over the old app
        self.assertEqual(len(list((app / "dist").glob("*.apk"))), 2)   # every version kept
        tags = sh("git -C /workspace/proxai/apps/proxai-test tag", check=True).stdout.split()
        self.assertEqual(sorted(tags), ["build-1", "build-2"])
        self.assertEqual(len(json.loads((app / "builds.json").read_text())), 2)

    def test_3_rollback_is_a_new_build_with_old_files(self):
        app = APPS / "proxai-test"
        r = build(extra="--rollback 1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["code"], 3)
        self.assertEqual(r["version"], "1.0")
        self.assertNotIn("v2</h1>", (app / "www" / "index.html").read_text())
        log = sh("git -C /workspace/proxai/apps/proxai-test log --oneline", check=True).stdout
        self.assertIn("Roll back to build 1", log)

    def test_4_progress_status(self):
        app = APPS / "proxai-test"
        p = sh('proxai-app-build --status testing "Running the app checks" /workspace/proxai/apps/proxai-test')
        self.assertEqual(p.returncode, 0, p.stderr)
        st = json.loads((app / "status.json").read_text())
        self.assertEqual((st["state"], st["message"]), ("testing", "Running the app checks"))
        self.assertEqual(st["last_build"]["code"], 3)                  # last good build kept

    def test_5_refusals(self):
        bad = copy_example("bad-app")
        meta = json.loads((bad / "proxai-app.json").read_text())
        meta["id"] = "bad-app"

        def expect(error_part, meta_change=None, setup=None):
            m = {**meta, **(meta_change or {})}
            (bad / "proxai-app.json").write_text(json.dumps(m))
            if setup:
                setup()
            r = build("bad-app")
            self.assertFalse(r["ok"], m)
            self.assertIn(error_part, r["error"])
            self.assertEqual(json.loads((bad / "status.json").read_text())["state"], "failed")

        expect("needs Nathan's approval", {"features": ["internet"]})
        expect("unknown feature", {"features": ["telepathy"]})
        expect("id must be", {"id": "Bad App!"})
        expect("color must be", {"color": "blue"})
        (bad / "icon.xml").write_text('<vector xmlns:android="http://schemas.android.com/apk/res/android">'
                                      '<path android:fillColor="@drawable/evil" android:pathData="M0,0"/></vector>')
        expect("can't reference other resources")
        (bad / "icon.xml").unlink()
        (bad / "www" / "leak").symlink_to("/etc/passwd")
        expect("symlinks aren't allowed")
        (bad / "www" / "leak").unlink()
        (bad / "www" / "index.html").unlink()
        expect("www/index.html is missing")
        self.assertFalse((bad / "dist").exists())

    def test_7_pc_stats_app_can_see_proxmobile(self):
        app = copy_example("pc-monitor")
        meta = json.loads((app / "proxai-app.json").read_text())
        meta.update({"id": "pc-monitor", "name": "PC Monitor", "features": ["pc-stats"]})
        (app / "proxai-app.json").write_text(json.dumps(meta))
        r = build("pc-monitor")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["permissions"], [])                        # no network permission
        xml = sh(f"aapt2 dump xmltree --file AndroidManifest.xml /workspace/proxai/apps/pc-monitor/{r['apk']}",
                 check=True).stdout
        self.assertIn("queries", xml)
        self.assertIn("com.proxai.hermes", xml)
        self.assertNotIn("android.permission.INTERNET", xml)

    def test_6_custom_icon(self):
        app = copy_example("icon-app")
        meta = json.loads((app / "proxai-app.json").read_text())
        meta.update({"id": "icon-app", "name": "Icon App", "features": []})
        (app / "proxai-app.json").write_text(json.dumps(meta))
        (app / "icon.xml").write_text(
            '<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="108dp" '
            'android:height="108dp" android:viewportWidth="108" android:viewportHeight="108">'
            '<path android:fillColor="#FFFFFFFF" android:pathData="M34,34h40v40h-40z"/></vector>')
        r = build("icon-app")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["permissions"], [])
        badging = sh(f"aapt2 dump badging /workspace/proxai/apps/icon-app/{r['apk']}", check=True).stdout
        self.assertNotIn("uses-permission", badging)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2, exit=False)
    finally:
        if not os.environ.get("KEEP"):
            shutil.rmtree(WS, ignore_errors=True)
