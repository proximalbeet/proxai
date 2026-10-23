"""
Scheduled jobs from Nathan's phone (decided 2026-10-23).

* "delete the pomodoro app at 2am", "every morning at 8 summarize my calendar", "in 2 hours check
  the PC temps": the hook finds the time, asks Nathan to confirm, and on his yes creates a Hermes
  cron job itself (the local model doesn't find the hidden cronjob tool).
* Saying yes when scheduling also approves the action: when the job fires, the app builder runs an
  approved build/update/delete without asking again (it reads approved jobs from the state file).
* The job's answer goes to the phone as a notification through the notice feed (notices.py), from
  the post_llm_call hook (cron runs carry task_id "cron:<job id>:...").
* "what's scheduled" lists them; "cancel the <...> job" removes one.

State: ~/.local/state/proxai/schedule/jobs.json {job_id: {action, display, created, approved}}.
"Remind me ..." stays with the phone's own reminders (exact alarms on the phone).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
STATE = Path(os.getenv("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser() / "schedule"
CONFIRM_SECS = 600
_ALLOWED_PLATFORMS = {"api_server", "cli"}   # plus Discord DMs

_spec = importlib.util.spec_from_file_location("proxai_schedule_notices", HERE / "notices.py")
notices = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notices)

_pending: dict[str, dict] = {}


# ---- state ----------------------------------------------------------------------------------

def load_jobs() -> dict:
    try:
        d = json.loads((STATE / "jobs.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_jobs(jobs: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / "jobs.json.tmp"
    tmp.write_text(json.dumps(jobs, indent=1), encoding="utf-8")
    os.replace(tmp, STATE / "jobs.json")


def is_approved_cron(task_id) -> dict | None:
    """The job record when task_id is a cron run of a job Nathan approved from the phone."""
    t = str(task_id or "")
    if not t.startswith("cron:"):
        return None
    rec = load_jobs().get(t.split(":")[1])
    return rec if rec and rec.get("approved") else None


# ---- reading the time out of a message --------------------------------------------------------

_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_T = r"(noon|midnight|(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?)"
_RECUR = re.compile(rf"\b(?:every|each)\s+(day|morning|evening|night|weekday|{_DAYS})\s*(?:at\s+)?{_T}"
                    rf"|\b(daily|weekdays)\s*(?:at\s+)?{_T}", re.I)
_EVERY_N = re.compile(r"\bevery\s+(\d{1,3})\s*(minutes?|mins?|hours?|hrs?)\b", re.I)
_IN_N = re.compile(r"\bin\s+(\d{1,3}|an?|one|two|three)\s*(minutes?|mins?|hours?|hrs?|days?)\b", re.I)
_ONCE = re.compile(rf"\b(?:(tonight|tomorrow|today)\s+)?at\s+{_T}(?:\s+(tonight|tomorrow|today))?", re.I)
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


def _clock(word, hh, mm, ampm, hint: str = "") -> tuple[int, int, bool]:
    """(hour 0-23, minute, meridiem_given)."""
    if word and word.lower() == "noon":
        return 12, 0, True
    if word and word.lower() == "midnight":
        return 0, 0, True
    h, m = int(hh), int(mm or 0)
    if h > 23 or m > 59:
        raise ValueError("bad time")
    ap = (ampm or "").replace(".", "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and h <= 12:
        if hint in ("evening", "night", "tonight") and h < 12 and h >= 5:
            h += 12
        elif hint in ("", "day", "today") and 1 <= h <= 5:
            h += 12            # "at 3" in a plain sentence: 3 pm
    return h, m, bool(ap) or h > 12


def _hermes_time(h: int, m: int) -> str:
    ap = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{ap}" if m else f"{h12}{ap}"


def parse_when(text: str, now: dt.datetime) -> tuple[str, str, tuple[int, int]] | None:
    """(Hermes schedule string, words for Nathan, span of the time phrase) or None."""
    m = _RECUR.search(text)
    if m:
        if m.group(1):
            unit, word, hh, mm, ap = m.group(1).lower(), m.group(2), m.group(3), m.group(4), m.group(5)
        else:
            unit, word, hh, mm, ap = m.group(6).lower(), m.group(7), m.group(8), m.group(9), m.group(10)
        h, mi, _ = _clock(word, hh, mm, ap, unit)
        if unit in ("weekday", "weekdays"):
            sched = f"weekdays at {_hermes_time(h, mi)}"
        elif unit in ("day", "morning", "evening", "night", "daily"):
            sched = f"every day at {_hermes_time(h, mi)}"
        else:
            sched = f"every {unit} at {_hermes_time(h, mi)}"
        return sched, sched, m.span()
    m = _EVERY_N.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        sched = f"every {n}{'m' if unit.startswith('m') else 'h'}"
        return sched, f"every {n} {'minutes' if unit.startswith('m') else 'hours'}", m.span()
    m = _IN_N.search(text)
    if m:
        n = _WORDNUM.get(m.group(1).lower(), None) or int(m.group(1))
        unit = m.group(2).lower()
        unit = "minutes" if unit.startswith("m") else "hours" if unit.startswith("h") else "days"
        return f"in {n} {unit}", f"in {n} {unit}", m.span()
    m = _ONCE.search(text)
    if m:
        day = (m.group(1) or m.group(6) or "").lower()
        h, mi, given = _clock(m.group(2), m.group(3), m.group(4), m.group(5), day)
        when = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if day == "tomorrow":
            when += dt.timedelta(days=1)
        elif when <= now:
            if not given and h < 12 and when + dt.timedelta(hours=12) > now:
                when += dt.timedelta(hours=12)      # "at 9" said at 2 pm: 9 pm
            else:
                when += dt.timedelta(days=1)
        words = when.strftime("%a %b %-d at %-I:%M %p")
        return when.strftime("%Y-%m-%dT%H:%M"), words, m.span()
    return None


_POLITE = re.compile(r"^\s*(hey\s+)?(proxai[,:]?\s+)?(please\s+)?((can|could|would|will) you\s+)?(please\s+)?"
                     r"(schedule\s+(a\s+job\s+)?(to\s+)?|set up a (cron )?job (to\s+)?)?", re.I)
_ACTION = re.compile(r"^(delete|remove|uninstall|build|make|create|update|change|fix|summari[sz]e|check|send|tell|"
                     r"give|run|post|clean|back ?up|turn|switch|look|search|find|list|show|write|add|rebuild|roll ?back|"
                     r"refresh|sync|start|stop|ping|text|message|notify|let me know|report)\b", re.I)
_SCHEDULE_WORDS = re.compile(r"\b(schedule|every|each|daily|weekdays|cron job)\b", re.I)
_QUESTION = re.compile(r"^\s*(how|what|why|when|where|who|which|is|are|do|does|did|should|was|were)\b|\?\s*$", re.I)
_REMIND = re.compile(r"\b(remind me|reminder|alarm|wake me)\b", re.I)
_RISKY = re.compile(r"\b(delete|remove|uninstall|build|make|create|update|change|fix|rebuild|roll ?back)\b", re.I)


def parse_request(text: str, now: dt.datetime) -> dict | None:
    """{"schedule", "when", "action"} when Nathan asks for something to happen at a time."""
    if _REMIND.search(text) or _QUESTION.search(text):
        return None
    try:
        found = parse_when(text, now)
    except ValueError:
        return None
    if not found:
        return None
    sched, when, (a, b) = found
    action = (text[:a] + " " + text[b:])
    action = _POLITE.sub("", action)
    action = re.sub(r"\s+", " ", action).strip(" ,.;:-")
    action = re.sub(r"^(and|then|to)\s+", "", action, flags=re.I).strip()
    if len(action) < 4:
        return None
    if not (_ACTION.search(action) or _SCHEDULE_WORDS.search(text)):
        return None
    return {"schedule": sched, "when": when, "action": action[:500]}


# ---- creating, listing, cancelling ------------------------------------------------------------

def _cron():
    from cron import jobs as cron_jobs   # Hermes's own cron store (the gateway's scheduler runs it)
    return cron_jobs


def create(req: dict) -> dict:
    prompt = (f"[Scheduled job: Nathan set this up from his phone on {dt.date.today().isoformat()} and approved "
              "it then, so do it now without asking. Reply with a short result (1-3 sentences) for his phone "
              f"notification.]\n{req['action']}")
    job = _cron().create_job(prompt=prompt, schedule=req["schedule"], name=f"phone: {req['action'][:50]}",
                             deliver="local")
    jobs = load_jobs()
    jobs[job["id"]] = {"action": req["action"], "when": req["when"], "schedule": job.get("schedule_display"),
                       "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "approved": True,
                       "risky": bool(_RISKY.search(req["action"]))}
    save_jobs(jobs)
    return job


def listing() -> list[dict]:
    mine = load_jobs()
    out = []
    for j in _cron().list_jobs():
        if j.get("id") in mine:
            out.append({"id": j["id"], "action": mine[j["id"]]["action"], "schedule": j.get("schedule_display"),
                        "next": j.get("next_run_at")})
    return out


def cancel(text: str) -> tuple[list[dict], list[dict]]:
    """(cancelled, candidates when unclear)."""
    active = listing()
    words = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 4} - {
        "cancel", "delete", "remove", "stop", "scheduled", "schedule", "job", "jobs", "that", "this", "the"}
    hits = [j for j in active if words & set(re.findall(r"[a-z0-9]+", j["action"].lower()))]
    if re.search(r"\ball\b", text, re.I):
        hits = active
    elif len(active) == 1 and not hits:
        hits = active
    if len(hits) != 1 and not re.search(r"\ball\b", text, re.I):
        return [], active
    jobs = load_jobs()
    for j in hits:
        _cron().remove_job(j["id"])
        jobs.pop(j["id"], None)
    save_jobs(jobs)
    return hits, []


# ---- hooks ----------------------------------------------------------------------------------

def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        return os.getenv(name, "")


def _where() -> tuple[str, bool]:
    if _session("HERMES_CRON_SESSION"):
        return "cron", False
    platform = _session("HERMES_SESSION_PLATFORM") or "cli"
    if platform == "discord":
        return ("dm", True) if _session("HERMES_SESSION_CHAT_TYPE") == "dm" else ("#channel", False)
    return platform, platform in _ALLOWED_PLATFORMS


def _plain(user_message) -> str:
    if isinstance(user_message, list):
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    return re.sub(r"\[[^\]]*\]", " ", str(user_message or "")).strip()


_YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|schedule it)\b[\s,.!]*(please|thanks)?[\s,.!]*$", re.I)
_LIST = re.compile(r"\b(what'?s|what is|list|show)\b.{0,20}\b(scheduled|my (cron )?jobs|scheduled jobs)\b", re.I)
_CANCEL = re.compile(r"\b(cancel|delete|remove|stop|unschedule)\b.{0,40}\b(scheduled|job|jobs|cron)\b", re.I)


def _on_pre_llm_call(session_id=None, user_message=None, parent_session_id=None, task_id=None, **_kwargs):
    if parent_session_id or str(task_id or "").startswith("cron:"):
        return None
    _w, allowed = _where()
    if not allowed:
        return None
    text = _plain(user_message)
    sid = session_id or ""
    pend = _pending.pop(sid, None)
    if pend and time.time() - pend["created"] < CONFIRM_SECS:
        if _YES.match(text):
            try:
                job = create(pend)
            except Exception as exc:
                return {"context": f"[schedule] Couldn't schedule it: {exc}. Tell Nathan in one sentence."}
            return {"context": f"[schedule] Done: scheduled \"{pend['action']}\" ({job.get('schedule_display') or pend['when']}, "
                               f"job {job['id']}). The result comes to his phone as a notification. Tell him in one "
                               "sentence; he can say \"what's scheduled\" or \"cancel the … job\"."}
        return {"context": "[schedule] He didn't say yes, so nothing was scheduled. Answer his message normally."}
    if _LIST.search(text):
        rows = listing()
        body = "\n".join(f"- {r['action']} · {r['schedule']} · next {str(r['next'])[5:16].replace('T', ' ')}"
                         for r in rows) or "Nothing is scheduled from the phone."
        return {"context": "[schedule] His scheduled jobs (tell him briefly):\n" + body}
    if _CANCEL.search(text):
        done, unclear = cancel(text)
        if done:
            return {"context": "[schedule] Done: cancelled " + "; ".join(f"\"{j['action']}\"" for j in done) +
                               ". Tell him in one sentence."}
        if unclear:
            return {"context": "[schedule] Not sure which job he means; ask him which: " +
                               "; ".join(f"\"{j['action']}\" ({j['schedule']})" for j in unclear)}
        return None
    req = parse_request(text, dt.datetime.now())
    if not req:
        return None
    req["created"] = time.time()
    _pending[sid] = req
    extra = (" Saying yes also approves that action then, without asking again." if _RISKY.search(req["action"]) else "")
    q = (f"Schedule \"{req['action']}\" for {req['when']}? It runs on the PC then and the result comes to your "
         f"phone as a notification.{extra} Say yes to schedule it.")
    return {"context": f"[schedule] Nothing is scheduled yet and nothing runs now. Do NOT call any tool or do the "
                       f"task now. Ask Nathan exactly: \"{q}\" and stop. The system reads his next message."}


def _on_post_llm_call(task_id=None, assistant_response=None, **_kwargs):
    """A phone-made job just answered: send that answer to the phone."""
    t = str(task_id or "")
    if not t.startswith("cron:"):
        return None
    rec = load_jobs().get(t.split(":")[1])
    if rec and str(assistant_response or "").strip():
        notices.post(f"⏰ {rec['action'][:70]}", str(assistant_response).strip(), kind="schedule")
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
