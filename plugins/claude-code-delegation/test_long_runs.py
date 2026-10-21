"""Integration test for the claude_code wait loop, against Hermes's real terminal tool and
process registry (local backend) with a fake cc-run. No Claude Code, no tokens.

    ~/.hermes/hermes-agent/venv/bin/python plugins/claude-code-delegation/test_long_runs.py

Before the fix, a run with the default 15-minute limit came back at once as
"Background process started" (Hermes's 600 s foreground cap) and was logged as a failure.
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES_AGENT = Path.home() / ".hermes/hermes-agent"  # before HOME is pointed at the temp dir
TMP = Path(tempfile.mkdtemp(prefix="cc-timeout-test-"))
BIN = TMP / "bin"
BIN.mkdir()
FAKE = BIN / "cc-run"
# Fake cc-run: reads the task from stdin, sleeps FAKE_SLEEP, prints a cc-run style result line.
FAKE.write_text("""#!/bin/bash
task="$(cat)"
echo "some noise on stdout"
sleep "${FAKE_SLEEP:-0}"
if [ -n "${FAKE_FAIL:-}" ]; then
  echo '{"is_error": true, "result": "401 Invalid bearer token"}'; exit 1
fi
python3 -c 'import json,sys; print(json.dumps({"is_error": False, "num_turns": 3, "session_id": "s1", "result": "did: " + sys.argv[1]}))' "$task"
""")
FAKE.chmod(FAKE.stat().st_mode | stat.S_IEXEC)

os.environ.update(HERMES_HOME=str(TMP / "home"), TERMINAL_ENV="local", TERMINAL_CWD=str(TMP),
                  PATH=f"{BIN}:{os.environ['PATH']}", HOME=str(TMP))
(TMP / "home").mkdir()
sys.path.insert(0, str(HERMES_AGENT))

spec = importlib.util.spec_from_file_location("ccdel", HERE / "__init__.py")
ccdel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccdel)
ccdel._LOG_PATH = TMP / "claude-code.log"
ccdel._post_audit = lambda text: None

from tools.process_registry import process_registry  # noqa: E402
from tools import interrupt  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if not cond and detail else ""))
    if not cond:
        failures.append(name)


# 1. Default 15-minute limit (was promoted to background -> instant false failure).
os.environ["FAKE_SLEEP"] = "3"
t0 = time.time()
r = json.loads(ccdel._handle_claude_code({"task": "build the calculator", "project": "calc"}))
took = time.time() - t0
check("default 15 min run returns the real result", r.get("ok") is True and r.get("result") == "did: build the calculator", r)
check("tool waited for the run (not an instant return)", took >= 3, f"{took:.1f}s")
check("turns reported", r.get("num_turns") == 3, r)
log = json.loads((TMP / "claude-code.log").read_text().splitlines()[-1])
check("log line says ok with no error", log["ok"] is True and log["error"] == "", log)

# 2. Max 30-minute limit, longer task text (base64 on stdin).
os.environ["FAKE_SLEEP"] = "1"
big = "x" * 15000
r = json.loads(ccdel._handle_claude_code({"task": big, "project": "calc", "timeout_minutes": 30}))
check("30 min limit + 15k task returns ok", r.get("ok") is True and r.get("result") == "did: " + big, str(r)[:200])

# 3. Claude Code error is reported as a failure.
os.environ.update(FAKE_SLEEP="0", FAKE_FAIL="1")
r = json.loads(ccdel._handle_claude_code({"task": "t", "project": "calc"}))
check("Claude Code error -> ok false with its message", r.get("ok") is False and "401" in r.get("error", ""), r)
del os.environ["FAKE_FAIL"]

# 4. Backstop deadline kills a run that outlives it.
os.environ["FAKE_SLEEP"] = "30"
t0 = time.time()
r = ccdel._run_and_wait("echo t | cc-run", 3)
check("backstop deadline -> exit 124 after ~3 s", r.get("exit_code") == 124 and time.time() - t0 < 10, r)
time.sleep(1)
running = [s for s in process_registry.list_sessions() if not s.get("exited", True)] \
    if hasattr(process_registry, "list_sessions") else []
check("no process left running after the deadline kill", not running, running)

# 5. /stop (interrupt) while waiting stops the run.
os.environ["FAKE_SLEEP"] = "30"
me = threading.get_ident()
threading.Timer(2, lambda: interrupt.set_interrupt(True, me)).start()
t0 = time.time()
r = ccdel._run_and_wait("echo t | cc-run", 60)
interrupt.set_interrupt(False, me)
check("interrupt -> exit 130 within a few seconds", r.get("exit_code") == 130 and time.time() - t0 < 10, r)

print("\nALL PASS" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
