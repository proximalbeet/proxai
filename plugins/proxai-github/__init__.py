"""
ProxAI on GitHub: read anything Nathan's token can see; write only with his yes, and never to his
own branches.

The token lives on the host (secrets/github.json in this profile, mode 600), never in the
sandbox, so the model can't use git or curl to push around these rules.

Reads (no asking): repos, repo (info + branches), tree, file, commits, issues, issue, prs, pr,
runs (CI), search (code).
Writes (Nathan's next message must be a plain yes; DMs, the phone app or the CLI only):
  create_issue, comment, create_branch (only "proxai/..." branches), commit_file (only on
  "proxai/..." branches), open_pr (draft, from a "proxai/..." branch).
Never, whatever the token allows: pushing to main or any branch not named proxai/..., merging,
deleting, force-pushing, .github/workflows files, repo settings.

Tokens (fine-grained, github.com/settings/personal-access-tokens):
  read  = Contents, Issues, Pull requests, Actions, Metadata: read-only
  write = Contents + Issues + Pull requests: read and write (optional; without it writes are off)
Every write and refusal goes to the security log (GITHUB) and ~/.local/state/proxai/github.log.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SECRETS = Path(os.getenv("PROXAI_GITHUB_SECRETS", str(HERE.parent.parent / "secrets" / "github.json")))
LOG = Path(os.getenv("PROXAI_GITHUB_LOG", str(Path.home() / ".local/state/proxai/github.log")))
API = os.getenv("PROXAI_GITHUB_API", "https://api.github.com")
OWNER = os.getenv("PROXAI_GITHUB_OWNER", "proximalbeet")
BRANCH_PREFIX = "proxai/"
CONFIRM_SECS = 600
MAX_FILE_CHARS = 20_000
MAX_ITEMS = 30
_ALLOWED_PLATFORMS = {"api_server", "cli"}   # plus Discord DMs

READS = ("repos", "repo", "tree", "file", "commits", "issues", "issue", "prs", "pr", "runs", "search")
WRITES = ("create_issue", "comment", "create_branch", "commit_file", "open_pr")
_pending: dict[str, dict] = {}


class Refused(Exception):
    pass


# ---- context ----------------------------------------------------------------------------------

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
        if _session("HERMES_SESSION_CHAT_TYPE") == "dm":
            return "dm", True
        return "#" + (_session("HERMES_SESSION_CHAT_NAME") or "channel"), False
    return platform, platform in _ALLOWED_PLATFORMS


def _log(rec: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    except OSError:
        pass


_security_log = None


def _audit(text: str, where: str) -> None:
    global _security_log
    try:
        if _security_log is None:
            spec = importlib.util.spec_from_file_location("proxai_github_guard",
                                                          HERE.parent / "discord-routing" / "guard.py")
            guard = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(guard)
            vault = Path(os.getenv("PROXAI_VAULT", str(Path.home() / "Documents/ProxVault")))
            _security_log = guard.SecurityLog(vault)
        _security_log.write("GITHUB", where, text)
    except Exception as exc:
        logger.warning("[github] security log failed: %s", exc)


# ---- GitHub REST ------------------------------------------------------------------------------

def _token(kind: str) -> str:
    try:
        return str(json.loads(SECRETS.read_text(encoding="utf-8")).get(kind) or "").strip()
    except (OSError, ValueError):
        return ""


def _request(method: str, path: str, *, token: str, body: dict | None = None, query: dict | None = None,
             raw: bool = False):
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github.raw" if raw else "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "proxai-hermes")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("message", "")
        except ValueError:
            msg = ""
        raise Refused(f"GitHub said {e.code}{': ' + msg if msg else ''}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise Refused(f"Couldn't reach GitHub: {e}") from None
    if raw:
        return data.decode("utf-8", "replace")
    return json.loads(data or b"null")


def _repo(name: str) -> str:
    name = str(name or "").strip().removeprefix("https://github.com/").strip("/")
    if name.count("/") == 0 and name:
        name = f"{OWNER}/{name}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name or "") or any(
            part in (".", "..") for part in name.split("/")):
        raise Refused("Give the repo as 'name' or 'owner/name'.")
    return name


def _clean_path(p: str) -> str:
    p = str(p or "").strip().lstrip("/")
    if not p or ".." in p.split("/"):
        raise Refused("Give a file path inside the repo.")
    return p


# ---- reads ------------------------------------------------------------------------------------

def _short_issue(i: dict) -> dict:
    return {"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
            "user": (i.get("user") or {}).get("login"), "updated": i.get("updated_at"),
            "draft": i.get("draft"), "url": i.get("html_url")}


def do_read(action: str, a: dict, token: str):
    if action == "repos":
        rows = _request("GET", "/user/repos", token=token, query={"per_page": 100, "sort": "updated"})
        return [{"repo": r["full_name"], "private": r["private"], "default_branch": r["default_branch"],
                 "updated": r["pushed_at"], "description": r.get("description")} for r in rows[:MAX_ITEMS]]
    repo = _repo(a.get("repo"))
    if action == "repo":
        r = _request("GET", f"/repos/{repo}", token=token)
        branches = _request("GET", f"/repos/{repo}/branches", token=token, query={"per_page": 100})
        return {"repo": r["full_name"], "private": r["private"], "default_branch": r["default_branch"],
                "description": r.get("description"), "open_issues": r.get("open_issues_count"),
                "branches": [b["name"] for b in branches][:100], "url": r["html_url"]}
    ref = str(a.get("ref") or "").strip()
    if action == "tree":
        ref = ref or _request("GET", f"/repos/{repo}", token=token)["default_branch"]
        t = _request("GET", f"/repos/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}", token=token,
                     query={"recursive": "1"})
        paths = [x["path"] for x in t.get("tree", []) if x.get("type") == "blob"]
        prefix = str(a.get("path") or "").strip("/")
        if prefix:
            paths = [p for p in paths if p.startswith(prefix + "/") or p == prefix]
        return {"ref": ref, "files": paths[:400], "more": max(0, len(paths) - 400)}
    if action == "file":
        text = _request("GET", f"/repos/{repo}/contents/{urllib.parse.quote(_clean_path(a.get('path')))}",
                        token=token, query={"ref": ref} if ref else None, raw=True)
        if not isinstance(text, str):
            raise Refused("That path is a folder; use tree.")
        cut = len(text) > MAX_FILE_CHARS
        return {"path": a.get("path"), "content": text[:MAX_FILE_CHARS], "truncated": cut}
    if action == "commits":
        q = {"per_page": MAX_ITEMS}
        if ref:
            q["sha"] = ref
        rows = _request("GET", f"/repos/{repo}/commits", token=token, query=q)
        return [{"sha": c["sha"][:7], "message": c["commit"]["message"].splitlines()[0][:120],
                 "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]} for c in rows]
    if action in ("issues", "prs"):
        state = a.get("state") if a.get("state") in ("open", "closed", "all") else "open"
        if action == "prs":
            rows = _request("GET", f"/repos/{repo}/pulls", token=token, query={"state": state, "per_page": MAX_ITEMS})
        else:
            rows = [i for i in _request("GET", f"/repos/{repo}/issues", token=token,
                                        query={"state": state, "per_page": MAX_ITEMS}) if "pull_request" not in i]
        return [_short_issue(i) for i in rows]
    if action in ("issue", "pr"):
        n = int(a.get("number") or 0)
        if n <= 0:
            raise Refused("Give the issue or PR number.")
        i = _request("GET", f"/repos/{repo}/issues/{n}", token=token)
        comments = _request("GET", f"/repos/{repo}/issues/{n}/comments", token=token, query={"per_page": MAX_ITEMS})
        out = {**_short_issue(i), "body": (i.get("body") or "")[:4000],
               "comments": [{"user": c["user"]["login"], "body": c["body"][:1500]} for c in comments]}
        if action == "pr":
            p = _request("GET", f"/repos/{repo}/pulls/{n}", token=token)
            files = _request("GET", f"/repos/{repo}/pulls/{n}/files", token=token, query={"per_page": 100})
            out.update(head=p["head"]["ref"], base=p["base"]["ref"], mergeable=p.get("mergeable"),
                       files=[{"file": f["filename"], "status": f["status"], "+": f["additions"],
                               "-": f["deletions"]} for f in files])
        return out
    if action == "runs":
        r = _request("GET", f"/repos/{repo}/actions/runs", token=token, query={"per_page": 10})
        return [{"workflow": x["name"], "branch": x["head_branch"], "status": x["status"],
                 "conclusion": x["conclusion"], "when": x["created_at"], "url": x["html_url"]}
                for x in r.get("workflow_runs", [])]
    if action == "search":
        q = str(a.get("query") or "").strip()
        if not q:
            raise Refused("Give a search query.")
        r = _request("GET", "/search/code", token=token, query={"q": f"{q} repo:{repo}", "per_page": 20})
        return [{"path": x["path"], "url": x["html_url"]} for x in r.get("items", [])]
    raise Refused(f"unknown action '{action}'")


# ---- writes -----------------------------------------------------------------------------------

def check_write(action: str, a: dict) -> tuple[str, str]:
    """(repo, one-line description) for an allowed write, else Refused. No network."""
    repo = _repo(a.get("repo"))
    branch = str(a.get("branch") or "").strip()
    if action in ("create_branch", "commit_file", "open_pr"):
        if not branch.startswith(BRANCH_PREFIX) or len(branch) <= len(BRANCH_PREFIX) \
                or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
            raise Refused(f"ProxAI only writes to its own branches, named {BRANCH_PREFIX}<something> "
                          "(never main or Nathan's branches).")
    if action == "create_issue":
        if not str(a.get("title") or "").strip():
            raise Refused("An issue needs a title.")
        return repo, f"open an issue in {repo}: \"{str(a['title']).strip()[:80]}\""
    if action == "comment":
        if int(a.get("number") or 0) <= 0 or not str(a.get("body") or "").strip():
            raise Refused("A comment needs the issue/PR number and a body.")
        return repo, f"comment on {repo}#{int(a['number'])}: \"{str(a['body']).strip()[:80]}\""
    if action == "create_branch":
        return repo, f"create branch {branch} in {repo}" + (f" from {a['base']}" if a.get("base") else "")
    if action == "commit_file":
        path = _clean_path(a.get("path"))
        if path.startswith(".github/"):
            raise Refused("ProxAI doesn't change .github/ (workflows can run with the repo's secrets).")
        if not isinstance(a.get("content"), str):
            raise Refused("commit_file needs the full new file content.")
        lines = a["content"].count("\n") + 1
        return repo, f"commit {path} ({lines} lines) to {branch} in {repo}: \"{str(a.get('message') or '')[:60]}\""
    if action == "open_pr":
        if not str(a.get("title") or "").strip():
            raise Refused("A PR needs a title.")
        return repo, f"open a draft PR in {repo} from {branch}: \"{str(a['title']).strip()[:80]}\""
    raise Refused(f"unknown action '{action}'")


def do_write(action: str, a: dict, token: str) -> dict:
    repo = _repo(a.get("repo"))
    branch = str(a.get("branch") or "").strip()
    info = _request("GET", f"/repos/{repo}", token=token)
    default = info["default_branch"]
    if branch and branch == default:
        raise Refused("Not the default branch.")
    if action == "create_issue":
        i = _request("POST", f"/repos/{repo}/issues", token=token,
                     body={"title": a["title"].strip(), "body": str(a.get("body") or "")})
        return {"url": i["html_url"], "number": i["number"]}
    if action == "comment":
        c = _request("POST", f"/repos/{repo}/issues/{int(a['number'])}/comments", token=token,
                     body={"body": a["body"]})
        return {"url": c["html_url"]}
    if action == "create_branch":
        base = str(a.get("base") or default)
        sha = _request("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(base, safe='/')}",
                       token=token)["object"]["sha"]
        _request("POST", f"/repos/{repo}/git/refs", token=token, body={"ref": f"refs/heads/{branch}", "sha": sha})
        return {"branch": branch, "from": base}
    if action == "commit_file":
        path = _clean_path(a["path"])
        q = urllib.parse.quote(path)
        try:
            sha = _request("GET", f"/repos/{repo}/contents/{q}", token=token, query={"ref": branch}).get("sha")
        except Refused:
            sha = None
        body = {"message": str(a.get("message") or f"ProxAI: update {path}"), "branch": branch,
                "content": base64.b64encode(a["content"].encode()).decode()}
        if sha:
            body["sha"] = sha
        r = _request("PUT", f"/repos/{repo}/contents/{q}", token=token, body=body)
        return {"commit": r["commit"]["sha"][:7], "url": r["content"]["html_url"]}
    if action == "open_pr":
        p = _request("POST", f"/repos/{repo}/pulls", token=token,
                     body={"title": a["title"].strip(), "head": branch, "base": str(a.get("base") or default),
                           "body": str(a.get("body") or "") + "\n\n_Opened by ProxAI with Nathan's OK._",
                           "draft": True})
        return {"url": p["html_url"], "number": p["number"]}
    raise Refused(f"unknown action '{action}'")


# ---- tool -------------------------------------------------------------------------------------

SCHEMA = {
    "name": "github",
    "description": (
        f"Nathan's GitHub (owner {OWNER}). Reads, no asking: repos, repo (info + branches), tree, file "
        "(path, ref), commits, issues, issue (number), prs, pr (number: files + comments), runs (CI), search "
        "(query). Writes need Nathan's yes: create_issue (title, body), comment (number, body), create_branch "
        f"(branch named {BRANCH_PREFIX}..., base), commit_file (branch {BRANCH_PREFIX}..., path, content, "
        "message), open_pr (draft; branch, title, body, base). A write returns a question: ask him exactly that "
        "and STOP; the system reads his reply. confirm only after the system says he said yes. Never main or "
        "his branches, never merge or delete. The sandbox has no GitHub login; use this tool, not git push/gh."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": list(READS + WRITES + ("confirm", "cancel"))},
        "repo": {"type": "string", "description": "'name' (owner proximalbeet) or 'owner/name'."},
        "path": {"type": "string"}, "ref": {"type": "string", "description": "Branch, tag or commit."},
        "number": {"type": "integer"}, "state": {"type": "string", "enum": ["open", "closed", "all"]},
        "query": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
        "branch": {"type": "string"}, "base": {"type": "string"}, "content": {"type": "string"},
        "message": {"type": "string", "description": "Commit message."}},
        "required": ["action"]},
}


def _handle(args: dict | None = None, **_kw) -> str:
    a = dict(args or {})
    action = str(a.get("action") or "")
    where, can_ask = _where()
    sid = _session("HERMES_SESSION_ID") or "cli"
    try:
        if action in READS:
            token = _token("read") or _token("write")
            if not token:
                return json.dumps({"ok": False, "error": "GitHub isn't set up yet (no token in "
                                   "secrets/github.json). Tell Nathan; the steps are in the plugin README."})
            return json.dumps({"ok": True, "result": do_read(action, a, token)})
        if action in WRITES:
            if not can_ask:
                raise Refused("GitHub changes need Nathan's yes, which only works in his DMs, the phone app or the CLI.")
            if not _token("write"):
                raise Refused("Writing to GitHub isn't set up (no write token). Reads still work.")
            repo, what = check_write(action, a)
            _pending[sid] = {"action": action, "args": a, "what": what, "where": where,
                             "created": time.time(), "confirmed": False}
            return json.dumps({"ok": True, "needs_confirmation": True,
                               "ask_nathan": f"Should I {what}? Say yes to go ahead."})
        if action == "confirm":
            p = _pending.get(sid)
            if not p:
                raise Refused("No pending GitHub change.")
            if time.time() - p["created"] > CONFIRM_SECS:
                _pending.pop(sid, None)
                raise Refused("That request expired. Ask again.")
            if not p["confirmed"]:
                raise Refused("Nathan hasn't said yes yet. Ask him and wait for his reply.")
            _pending.pop(sid, None)
            check_write(p["action"], p["args"])      # the rules again, right before writing
            result = do_write(p["action"], p["args"], _token("write"))
            _audit(f"{p['what']} · approved at {p['where']} · {result}", p["where"])
            _log({"action": p["action"], "what": p["what"], "ok": True, "result": result})
            return json.dumps({"ok": True, "done": p["what"], "result": result})
        if action == "cancel":
            return json.dumps({"ok": True, "cancelled": bool(_pending.pop(sid, None))})
        raise Refused(f"unknown action '{action}'")
    except Refused as exc:
        if action in WRITES or action == "confirm":
            _audit(f"refused {action}: {exc}", where)
            _log({"action": action, "ok": False, "error": str(exc)})
        return json.dumps({"ok": False, "error": str(exc)})


# ---- hooks ------------------------------------------------------------------------------------

_YES = re.compile(r"^\s*(yes|yeah|yep|yup|ya|sure|ok|okay|confirm|confirmed|approve|approved|do it|go ahead|"
                  r"go for it|ship it|send it)\b[\s,.!]*(please|now|do it|go ahead)?[\s,.!]*$", re.I)
_NO_WORDS = re.compile(r"\b(no|nah|nope|don'?t|not|wait|cancel|stop|hold on)\b", re.I)
_ASK = re.compile(r"\b(github|git ?hub|repos?|repository|pull requests?|prs?|issues?|commits?|branch(es)?|"
                  r"ci|workflow runs?|actions run)\b", re.I)


def _plain(user_message) -> str:
    if isinstance(user_message, list):
        user_message = " ".join(p.get("text", "") for p in user_message if isinstance(p, dict))
    return re.sub(r"\[[^\]]*\]", " ", str(user_message or "")).strip()


def _example(action: str, **extra) -> str:
    args = json.dumps({"action": action, **extra})
    return f'tool_call({{"calls": [{{"name": "github", "arguments": {args}}}]}})'


def _on_pre_llm_call(session_id=None, user_message=None, parent_session_id=None, **_kwargs):
    if parent_session_id:
        return None
    text = _plain(user_message)
    p = _pending.get(session_id or "")
    if p and not p["confirmed"]:
        if time.time() - p["created"] <= CONFIRM_SECS and _YES.match(text) and not _NO_WORDS.search(text):
            p["confirmed"] = True
            return {"context": f"[github] Nathan said yes to: {p['what']}. Call {_example('confirm')} now, "
                               "then report the result (link)."}
        _pending.pop(session_id, None)
        _audit(f"declined: {p['what']}", p["where"])
        return {"context": f"[github] Nathan did not approve, so this was NOT done: {p['what']}."}
    if _ASK.search(text):
        return {"context": "[github] For GitHub use the github tool (the sandbox has no GitHub login), e.g. "
                           f"{_example('prs', repo='proxai')} or {_example('file', repo='ProxMobile', path='README.md')}. "
                           "Changes (issues, comments, proxai/... branches, draft PRs) need his yes; never main "
                           "or his branches."}
    return None


def register(ctx) -> None:
    ctx.register_tool(name="github", toolset="proxai_github", schema=SCHEMA, handler=_handle, emoji="🐙",
                      description="Read Nathan's GitHub; changes only with his yes, only on proxai/ branches")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
