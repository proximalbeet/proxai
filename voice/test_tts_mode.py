"""Phase 10: tts_pipeline.py follows the server/personal mode. Uses a fake voice server; the real
one and the GPU are never touched.

Run with the Chatterbox venv (numpy + soundfile):
  voice/.venv-chatterbox/bin/python voice/test_tts_mode.py
"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="tts-mode-test-"))
os.environ["PROXAI_STATE_DIR"] = str(TMP)
os.environ["PROXAI_ENV_FILE"] = str(TMP / ".env")
(TMP / ".env").write_text("API_SERVER_KEY=secret # comment\n")
sys.path.insert(0, str(HERE))
import tts_pipeline as tp  # noqa: E402


class FakeVoice(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeVoice.calls.append((self.headers.get("Authorization"), body))
        buf = io.BytesIO()
        sf.write(buf, np.zeros(2400, dtype=np.float32), 24000, format="WAV")
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class TestTtsMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVoice)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        tp.VOICE_SERVER_URL = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeVoice.calls.clear()
        self.inp, self.out = TMP / "in.txt", TMP / "out.wav"
        self.inp.write_text("Yo. It's Ye.")
        self.out.unlink(missing_ok=True)

    def mode(self, hermes):
        (TMP / "mode.json").write_text(json.dumps({"mode": "x", "hermes": hermes}))

    def run_main(self):
        sys.argv = ["tts_pipeline.py", str(self.inp), str(self.out), "kanye"]
        tp.main()

    def test_personal_mode_is_text_only(self):
        self.mode({"voice": False})
        with self.assertRaises(SystemExit) as cm:
            self.run_main()
        self.assertIn("personal mode", str(cm.exception.code))
        self.assertEqual(FakeVoice.calls, [])

    def test_server_mode_uses_warm_voice_server(self):
        self.mode({"voice": True, "voice_warm": True})
        self.run_main()
        self.assertTrue(self.out.exists())
        auth, body = FakeVoice.calls[0]
        self.assertEqual(auth, "Bearer secret")
        self.assertEqual(body["voice"], "kanye")

    def test_long_text_split_for_the_server(self):
        self.inp.write_text(" ".join(["This sentence is about forty characters long."] * 60))
        self.mode({"voice": True, "voice_warm": True})
        self.run_main()
        self.assertGreater(len(FakeVoice.calls), 1)
        self.assertTrue(all(len(b["text"]) <= 1200 for _, b in FakeVoice.calls))

    def test_settings(self):
        (TMP / "mode.json").unlink(missing_ok=True)
        self.assertEqual(tp.mode_voice_settings(), {})
        self.assertEqual(tp._api_key(), "secret")


if __name__ == "__main__":
    unittest.main(verbosity=1)
