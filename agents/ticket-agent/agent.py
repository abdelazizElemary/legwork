#!/usr/bin/env python3
"""Ticket agent. It checks the tickets assigned to you, and after you approve one it implements it on fresh
branches from the latest default branch and opens a draft PR per repo. The tracker stays read-only.

ptk cycle                       one tracker poll, what the scheduler runs every few minutes
ptk list                        your tickets and where each one stands
ptk show <KEY>                  the ticket, the agent's check and any PRs
ptk check <KEY>                 run the context check now
ptk context <KEY> "<text>" [--implement]   add context, then check again (or implement)
ptk approve <KEY> ["notes"]     implement it and open draft PRs
ptk finalize <KEY> [--dry-run]  run the PR step again for an implemented ticket
ptk prs [<KEY>]                 read the PRs' state from GitHub now (draft, ready, approved, merged, closed)
ptk rework <KEY> ["what to change"] [--image FILE ...]   more work on the same PRs (or a follow-up PR), with screenshots
ptk skip <KEY> | unskip <KEY>
ptk login                       sign in to the tracker in the agent's Chrome window (Jira browser auth only)
ptk pause | resume | doctor | dashboard [stop|--no-open]
ptk settings [check|implement|diagnose] [--model M] [--effort E]   show or change the defaults
ptk check|approve <KEY> --model M --effort E               the same, for one ticket
ptk models                      re-check which models your Claude setup allows
"""

import calendar
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

HOME = Path.home()
BASE = Path(__file__).resolve().parent
LIB = BASE.parent / "agent-lib"
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(LIB))
import osglue as OS  # noqa: E402
import runsettings as RS  # noqa: E402
import progress  # noqa: E402
import trackers  # noqa: E402

STATE, LOGS, TICKETS, RUNS = BASE / "state", BASE / "logs", BASE / "tickets", BASE / "runs"
PAUSE, LOCK, LAST_CYCLE = BASE / "paused", BASE / ".lock", BASE / "last_cycle"
TRACKER_STATUS, DASH, IMPL_SLOT = BASE / "tracker_status.json", BASE / "dashboard.json", BASE / ".implement.lock"
RUN_SETTINGS, PROGRESS = BASE / "run_settings.json", BASE / "progress"
JOBS = ("check", "implement", "diagnose", "tell")
VENV_PY = BASE / ".venv" / "bin" / "python"
CFG = json.loads((BASE / "config.json").read_text())
SKILL = Path(os.path.expanduser(CFG.get("skill_dir") or str(BASE.parent / "skills" / "ticket-work")))
REVIEW_SKILL = Path(os.path.expanduser(CFG.get("review_skill_dir") or str(BASE.parent / "skills" / "pr-review")))
LABEL, SCHEDULER = CFG["service_label"], CFG.get("scheduler", "none")
CLAUDE = os.path.expanduser(CFG["claude_bin"])
REAL_GH, REAL_GIT = os.path.expanduser(CFG["gh_bin"]), os.path.expanduser(CFG["git_bin"])
ORG, HOST = CFG["org"], CFG["host"]
WS = Path(os.path.expanduser(CFG["workspace"]))
RUN_PATH = ":".join(os.path.expanduser(p) for p in CFG["path"].split(":"))
CRED_VARS = CFG.get("shell_env", [])
TOKEN_ENV = CFG["tracker"].get("token_env")
if TOKEN_ENV and not os.environ.get(TOKEN_ENV):
    os.environ.update(OS.shell_env([TOKEN_ENV], HOME))
TR = trackers.load({**CFG["tracker"], "gh_bin": REAL_GH, "path": RUN_PATH}, BASE)
SignInNeeded = trackers.SignInNeeded
CONV = {"pr_title": "{type}({key}): {summary}", "commit_subject": "{type}({key}): {summary}",
        "branch": "{type}/{key}-{slug}", **CFG.get("conventions", {})}
TYPES = "fix|feat|chore|docs|refactor|test|perf"
SENSITIVE = re.compile(r"(^|/)(\.env(\.(?!example$)[^/]*)?|id_rsa[^/]*|[^/]*\.(pem|key|p12|pfx)|\.jira_cookies[^/]*)$")
NOISE = re.compile(r"(^|/)(node_modules|\.npm-install\.log|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|coverage|\.coverage[^/]*|\.next|dist|build)(/|$)")
# phases: new checking needs_context ready already_done not_implementable approved implementing
#         pr_open needs_attention failed skipped closed
WORKING = ("checking", "implementing")


# ---------- plumbing ----------

def attribution():
    """attribution from the config, as install wrote it into the review policy (refreshed on every install)."""
    try:
        a = json.loads((REVIEW_SKILL / "references" / "policy.json").read_text()).get("attribution") or {}
    except (OSError, ValueError):
        a = {}
    return {"mode": "none", **a}


def with_footer(text):
    """Under attribution.mode disclose, PR descriptions and replies end with the footer, once."""
    a = attribution()
    if a["mode"] != "disclose" or a["footer"] in text:
        return text
    return text.rstrip() + "\n\n" + a["footer"]


def attribution_error(repo, sha, msg, merge):
    """"none": no commit mentions a tool. "disclose": every commit of a run carries the trailer the git guard adds."""
    a = attribution()
    if a["mode"] == "disclose":
        if not merge and a["commit_trailer"] not in msg:
            return f"{repo}: commit {sha[:7]} is missing the attribution trailer"
    elif re.search(r"co-authored-by|generated with|claude|anthropic", msg, re.I):
        return f"{repo}: commit {sha[:7]} message has attribution in it"
    return None


def now():
    return time.strftime("%Y-%m-%d %H:%M")


def log(msg):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "agent.log", "a") as f:
        f.write(f"{now()} {msg}\n")


def claude_env(extra=None):
    env = OS.claude_env(CRED_VARS, HOME, RUN_PATH, extra)
    # the tracker token is the agent's, the Claude run never needs it
    if TOKEN_ENV and TOKEN_ENV not in CRED_VARS:
        env.pop(TOKEN_ENV, None)
    return env


def run(cmd, cwd=None, check=True, timeout=600, input=None):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input,
                         env={**os.environ, "PATH": RUN_PATH, "GH_HOST": HOST})
    if check and out.returncode != 0:
        raise RuntimeError(f"{' '.join(str(c) for c in cmd[:4])}: {(out.stderr or out.stdout).strip()[:400]}")
    return out


def git(*args, **kw):
    return run([REAL_GIT, *args], **kw).stdout.strip()


def notify(title, text):
    if CFG.get("notify", True):
        OS.notify(title, text)


def load(key):
    f = STATE / f"{key}.json"
    return json.loads(f.read_text()) if f.exists() else None


USER_FIELDS = ("context", "run_settings", "fix_request", "rework_request", "tell")


def touch_user(st):
    """Mark an edit the user made (context, model choice), so a run saving its older copy can't erase it."""
    st["user_rev"] = (st.get("user_rev") or 0) + 1


def save(st):
    STATE.mkdir(parents=True, exist_ok=True)
    disk = load(st["key"])
    if disk and (disk.get("user_rev") or 0) > (st.get("user_rev") or 0):
        st.update({k: disk[k] for k in USER_FIELDS if k in disk})
        st["user_rev"] = disk["user_rev"]
    st["state_updated"] = now()
    tmp = STATE / f"{st['key']}.json.tmp"
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE / f"{st['key']}.json")


def all_states():
    return [json.loads(f.read_text()) for f in sorted(STATE.glob("*.json"))]


def set_phase(st, phase, **fields):
    old = st.get("phase")
    st.update({"phase": phase, **fields})
    save(st)
    if old != phase:
        log(f"{st['key']} {old} -> {phase}")
    return st


def lock_file(name):
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / f".{name}.lock"


def is_running(key):
    return OS.lock_held(lock_file(key))


# ---------- parallel runs: a pool of slots per job, changeable from the desk ----------

DEFAULT_PARALLEL = {"check": 3, "implement": 2, "diagnose": 3, "tell": 3}
MAX_SLOTS = 8


def parallel_limit(job):
    """How many runs of this job may go at once: the desk's choice, else the config, else the default."""
    chosen = (RS.load(RUN_SETTINGS, JOBS).get("parallel") or {}).get(job)
    n = chosen or (CFG.get("max_parallel") or {}).get(job) or DEFAULT_PARALLEL[job]
    return max(1, min(MAX_SLOTS, int(n)))


def slot_path(job, i):
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / f".{job}.slot{i}.lock"


def take_slot(job):
    """An open file holding one of the job's slots, or None when they're all busy. Closing it frees the slot."""
    for i in range(parallel_limit(job)):
        f = open(slot_path(job, i), "a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            f.close()
    return None


def running_count(job):
    return sum(OS.lock_held(slot_path(job, i)) for i in range(MAX_SLOTS) if slot_path(job, i).exists())


def implementing_now():
    return running_count("implement") > 0


QUEUED = {"check": "check_queued", "implement": "approved"}


def start_queued():
    """Start queued tickets while slots are free, the highest priority in the current sprint first."""
    for job, phase in QUEUED.items():
        free = parallel_limit(job) - running_count(job)
        if free <= 0:
            continue
        queued = [s for s in all_states() if s.get("phase") == phase and not is_running(s["key"])]
        queued.sort(key=lambda s: (sprint_rank(s.get("sprint")), priority_rank(s.get("priority")), s.get("state_updated") or ""))
        for st in queued[:free]:
            spawn(f"_{job}", st["key"])


def check_key(key):
    key = key.strip().upper()
    if not trackers.KEY_RE.match(key):
        sys.exit(f"not a ticket key: {key}")
    return key


def code_repos():
    return [p for p in sorted(WS.iterdir()) if (p / ".git").exists()] if WS.exists() else []


def repo_dir(repo):
    d = WS / repo
    if (d / ".git").exists():
        return d
    d = WS / ".local" / "clones" / repo
    if not (d / ".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", f"git@{HOST}:{ORG}/{repo}.git", str(d))
    return d


def default_branch(rdir, repo):
    return CFG.get("base_branches", {}).get(repo) or \
        git("-C", str(rdir), "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")


def title_re(key):
    """The configured PR title format as a regex, e.g. '{type}({key}): {summary}'."""
    rx = re.escape(CONV["pr_title"])
    for name, part in (("type", f"({TYPES})"), ("key", re.escape(key)), ("summary", ".{5,90}"),
                       ("num", re.escape(key.split("-")[1]))):
        rx = rx.replace(re.escape("{" + name + "}"), part)
    return rx


def branch_name(key, type_="<type>", slug="<slug>"):
    return CONV["branch"].replace("{type}", type_).replace("{key}", key.lower()).replace("{slug}", slug)


def branch_re(key):
    """The configured branch format as a regex, e.g. '{type}/{key}-{slug}'."""
    rx = re.escape(CONV["branch"])
    for name, part in (("type", f"({TYPES})"), ("key", re.escape(key.lower())), ("slug", "[a-z0-9]+(-[a-z0-9]+)*")):
        rx = rx.replace(re.escape("{" + name + "}"), part)
    return rx


# ---------- tracker ----------

def same_second(ts):
    """Some trackers answer with whole seconds in search and milliseconds elsewhere, so compare to the second."""
    return re.sub(r"\.\d+", "", ts or "")


AUTO_RECHECK_COOLDOWN_S = 6 * 3600


def tracker_status(ok, **extra):
    TRACKER_STATUS.write_text(json.dumps({"ok": ok, "at": now(), **extra}))


def fill(fmt, key):
    return fmt.replace("{key}", key).replace("{num}", key.split("-")[1]).replace("{type}", "<type>").replace("{summary}", "<summary>")


# ---------- the repo's own PR template ----------

TEMPLATE_PATHS = (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md", "pull_request_template.md",
                  "PULL_REQUEST_TEMPLATE.md", "docs/pull_request_template.md", "docs/PULL_REQUEST_TEMPLATE.md")


def pr_template(repo, rdir=None):
    """The repo's PR template on its default branch, as (path, text), or None. ticket.pr_template: "repo" (the
    default) uses it when the repo has one, "none" never does."""
    if CFG.get("pr_template", "repo") == "none":
        return None
    rdir = rdir or repo_dir(repo)
    base = default_branch(rdir, repo)
    for path in TEMPLATE_PATHS:
        out = run([REAL_GIT, "-C", str(rdir), "show", f"origin/{base}:{path}"], check=False)
        if out.returncode == 0 and out.stdout.strip():
            return path, out.stdout
    return None


def _heading(line):
    return re.sub(r"[^a-z0-9]+", " ", line.lstrip("#").lower()).strip()


def links_ticket(st, body):
    """The body already names the ticket (e.g. under the template's related issues), so no extra ticket line."""
    return any(re.search(re.escape(n) + r"(?!\d)", body or "") for n in TR.mentions(st))


def _sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s+\S", line):
            cur = _heading(line)
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


# the issue link a template leaves for the author to fill in, e.g. "Fixes #issue-number"
PLACEHOLDER = re.compile(r"#(ticket|issue)[-_ ]?(number|id)\b", re.I)


def template_errors(repo, body, template):
    """A body that follows the template: every section kept, in order, and filled. A section is filled when it has
    text or a ticked box beyond the template's own lines and hints; "(if applicable)" sections may stay empty."""
    errs = []
    want, have = _sections(template), _sections(body)
    missing = [w for w in want if w not in have]
    if missing:
        return [f"{repo} PR body: the repo's PR template sections are missing: {', '.join(missing)}"]
    if [h for h in have if h in want] != list(want):
        errs.append(f"{repo} PR body: the PR template sections are out of order, keep the template's order")
    for name, lines in want.items():
        if "if applicable" in name or "optional" in name:
            continue
        stock = {l.strip() for l in lines}
        content = re.sub(r"<!--.*?-->", "", "\n".join(have[name]), flags=re.S)
        own = [l for l in content.splitlines() if l.strip() and l.strip() not in stock and l.strip() != "---"
               and not re.match(r"^\s*[-*]\s+\[ \]", l)]
        ticked = any(re.match(r"^\s*[-*]\s+\[[xX]\]", l) for l in content.splitlines())
        if not own and not ticked:
            errs.append(f"{repo} PR body: the template's '{name}' section is empty, fill it in")
    left = PLACEHOLDER.search(body)
    if left:
        errs.append(f"{repo} PR body: a template placeholder is left in ({left.group(0)})")
    return errs


def write_conventions(d, st):
    key = st["key"]
    bases = CFG.get("base_branches") or {}
    allowed = ", ".join(CFG["repos"]) if CFG.get("repos") else f"any repo under {WS} whose origin is {HOST}:{ORG}"
    a = CFG["author"]
    lines = [
        f"# Conventions for {key}", "",
        f"- Workspace: {WS}. Repos you may change: {allowed}.",
        f"- Branch: `{branch_name(key)}`, made only with `{SKILL}/scripts/ticket_worktree.sh`"
        + (f" and `--branch-format '{CONV['branch']}'`" if CONV["branch"] != "{type}/{key}-{slug}" else "") + ". "
        f"Types: {TYPES.replace('|', ', ')}.",
        "- Base: the repo's default branch (origin/HEAD)" + (", except " + ", ".join(f"{r}: {b}" for r, b in bases.items()) if bases else "") + ". "
        "Pass it with `--base` when it isn't origin/HEAD.",
        f"- Commit subject: `{fill(CONV['commit_subject'], key)}`. Every commit names {key}" +
        (f" (or {' or '.join(TR.mentions(st)[1:])})" if len(TR.mentions(st)) > 1 else "") + ".",
        f"- PR title: `{fill(CONV['pr_title'], key)}`, checked before anything is pushed.",
        f"- Author: {a['name']} <{a['email']}>, as both author and committer. " +
        ("No trailers, no attribution." if attribution()["mode"] == "none" else
         f"The git guard adds `{attribution()['commit_trailer']}` to every commit and the agent adds the disclosure to PR "
         "text and replies: don't write either yourself."),
    ]
    templates = []
    for rdir in code_repos():
        try:
            t = pr_template(rdir.name, rdir)
        except Exception:
            t = None
        if t:
            templates.append(f"{rdir.name}: `{t[0]}` (`git -C {rdir} show origin/{default_branch(rdir, rdir.name)}:{t[0]}`)")
    if templates:
        lines.append("- PR description: fill in the repo's own PR template, " + "; ".join(templates) + ". Keep every "
                     "section in its order, replace each <!-- hint --> with the content, tick the boxes that apply, and "
                     "put the ticket under the related-issues section. The agent checks it before pushing.")
    line = TR.issue_line(st)
    if line:
        lines.append(f"- If the PR template has no place for the ticket, the agent appends `{line}`; don't add it yourself.")
    (d / "conventions.md").write_text("\n".join(lines) + "\n")


def prepare_ticket(key, st):
    """Fresh ticket folder for a run: ticket.md, issue.json, attachments, context.md, conventions.md."""
    d = TICKETS / key
    d.mkdir(parents=True, exist_ok=True)
    info = TR.fetch(st, d)
    ctx = st.get("context") or []
    (d / "context.md").write_text("\n\n".join(f"## {c['at']}\n\n{c['text']}" for c in ctx) or "(none yet)\n")
    if st.get("assessment"):
        (d / "assessment.json").write_text(json.dumps(st["assessment"], indent=1))
    write_conventions(d, st)
    st.update({"summary": info.get("summary"), "tracker_updated": info.get("updated")})
    return d


# ---------- Claude runs ----------

def settings(read_only=False):
    scripts = [f"{root}/scripts/{s}" for root in (str(SKILL), "~/.claude/skills/ticket-work")
               for s in ("ticket_worktree.sh",)]
    scripts += [f"{root}/scripts/{s}" for root in (str(REVIEW_SKILL), "~/.claude/skills/pr-review")
                for s in ("pr_worktree.sh", "diff_coverage.py", "post_review.py")]
    return {"permissions": {
        "allow": [f"Bash({r} {s}:*)" for s in scripts for r in ("bash", "python3")],
        "deny": ["Bash(git push:*)", "Bash(gh pr create:*)", "Bash(gh pr merge:*)", "Bash(gh pr review:*)",
                 "Bash(gh pr comment:*)", "Bash(gh pr edit:*)", "Bash(gh issue comment:*)", "Bash(gh issue edit:*)"]
                + [f"{tool}(/{p}/**)" for p in code_repos() for tool in ("Edit", "Write")]
                + (["Edit", "MultiEdit", "NotebookEdit"] + [f"Bash(git {c}:*)" for c in
                   ("commit", "reset", "checkout", "switch", "restore", "stash", "add", "rm", "rebase", "merge", "am",
                    "cherry-pick", "branch", "worktree", "clean")] if read_only else [])}}


def job_choice(st, job):
    """The ticket's own choice for this job, else the default for the job, else the user's Claude default."""
    return RS.resolve(RS.load(RUN_SETTINGS, JOBS), job, (st.get("run_settings") or {}).get(job))


def set_job_override(st, job, model=None, effort=None):
    model, effort = RS.clean(model, effort, RS.load(RUN_SETTINGS, JOBS))
    rs = st.setdefault("run_settings", {})
    entry = {k: v for k, v in (("model", model), ("effort", effort)) if v}
    if entry:
        rs[job] = entry
    else:
        rs.pop(job, None)
    return entry


def claude_run(prompt, budget, timeout_min, log_path, extra_dirs, job, st, resume=None, read_only=False):
    """One headless run. Returns (result event, final text, facts about the run for a failure report)."""
    env = claude_env({"PATH": f"{LIB / 'shims'}:{RUN_PATH}", "GH_HOST": HOST, "PR_REVIEW_NO_POST": "1"})
    model, effort = job_choice(st, job)
    cmd = [CLAUDE, "-p", prompt, "--permission-mode", "auto", "--permission-prompts", "none",
           "--output-format", "json", "--max-budget-usd", str(budget), "--settings", json.dumps(settings(read_only)),
           "--add-dir", "/tmp"] + RS.flags(model, effort) + (["--resume", resume] if resume else [])
    for d in extra_dirs:
        cmd += ["--add-dir", str(d)]
    prog_path = PROGRESS / (f"{st['key']}.json" if job not in ("diagnose", "tell") else f"{st['key']}.{job}.json")
    timed_out = False
    started = time.time()
    # streamed, so the ticket desk can show each step while it runs
    try:
        # the run follows the ticket's model and effort live: a change on the desk switches it within seconds
        live = lambda: RS.concrete(*job_choice(load(st["key"]) or st, job))
        res, stderr = progress.run_claude(cmd, WS, env, timeout_min * 60, prog_path, job, desired=live,
                                          describe=lambda m, e: RS.describe(m, e, RS.load(RUN_SETTINGS, JOBS)))
    except subprocess.TimeoutExpired:
        res, stderr, timed_out = {}, "", True
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(res) if res else stderr)
    used = next(iter((res.get("modelUsage") or {}).keys()), None)
    u = RS.user_default()
    st[f"{job}_model"] = RS.label(used) if used else RS.name_of(model or u.get("model"))
    st[f"{job}_effort"] = effort or u.get("effort")
    prog = progress.view(prog_path, 12) or {}
    text = (res.get("result") or stderr or "").strip()
    facts = {"job": job, "session_id": res.get("session_id") or prog.get("session_id") or resume,
             "timed_out": timed_out, "timeout_min": timeout_min, "subtype": res.get("subtype"),
             "is_error": bool(res.get("is_error")), "cost": res.get("total_cost_usd"), "budget": budget,
             "duration_s": int(time.time() - started), "turns": res.get("num_turns"), "log": str(log_path),
             "result_text": text[-1500:], "stderr": (stderr or "")[-1500:], "model": st[f"{job}_model"],
             "effort": st[f"{job}_effort"], "last_steps": [x.get("text") for x in (prog.get("steps") or [])][-8:]}
    return res, text, facts


UNATTENDED = ("This run is unattended: the ticket agent started it and nobody is watching. Never push, open PRs, "
              "write to the tracker or ask questions (those are blocked anyway). Write the output file exactly as "
              "the skill describes, then end with the RESULT line.")


def fix_prompt(st, fix, what):
    """The prompt that resumes a failed run with the fix the user approved on the desk."""
    f = st.get("failure") or {}
    d = st.get("diagnosis") or {}
    why = d.get("why") or (f.get("explained") or {}).get("detail") or f.get("message") or ""
    return (f"The last {what} run for {st['key']} failed. What went wrong: {(f.get('explained') or {}).get('title') or f.get('message')}. "
            f"Why: {why}\n\nFix it like this: {fix['instructions']}\n\n")


def do_check(key):
    lock = open(lock_file(key), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return load(key)
    st = load(key) or {"key": key}
    slot = take_slot("check")
    if not slot:
        return set_phase(st, "check_queued", error=None)
    fix = st.pop("fix_request", None)
    set_phase(st, "checking", error=None, started=now())
    facts = {}
    try:
        d = prepare_ticket(key, st)
        out = d / "assessment.out.json"
        out.unlink(missing_ok=True)
        task = f"/ticket-work assess {key} {d} {out}"
        resume = st.get("check_session") if fix else None
        prompt = (fix_prompt(st, fix, "check") + f"Then finish the check and write the result to {out}." if fix and resume
                  else task + (f"\n\nNote from the developer after the last attempt failed: {fix['instructions']}" if fix else ""))
        res, text, facts = claude_run(f"{prompt}\n\n{UNATTENDED}", CFG["check_budget_usd"], CFG["check_timeout_minutes"],
                                      LOGS / f"{key}-check-{int(time.time())}.json", [d], "check", st, resume=resume)
        st["cost_check"] = (st.get("cost_check") or 0) + (res.get("total_cost_usd") or 0)
        st["check_session"] = facts["session_id"] or st.get("check_session")
        if not out.exists():
            return record_failure(st, "check", f"the check wrote no result: {text[-300:]}", facts)
        a = json.loads(out.read_text())
        out.replace(d / "assessment.json")
        phase = {"ready": "ready", "needs_context": "needs_context", "already_done": "already_done",
                 "not_implementable": "not_implementable"}.get(a.get("verdict"), "needs_context")
        st.update({"assessment": a, "checked_at": now(), "checked_at_ts": time.time(), "failure": None, "diagnosis": None,
                   "checked_tracker_updated": same_second(st.get("tracker_updated"))})
        set_phase(st, phase)
        if phase == "needs_context":
            notify(f"{key} needs your input", f"{len(a.get('questions') or [])} questions on the ticket desk")
        elif phase == "ready":
            notify(f"{key} is ready to approve", (a.get("summary") or "")[:140])
        return st
    except SignInNeeded as exc:
        tracker_status(False, error=str(exc))
        return set_phase(st, "new", error=f"{TR.name} sign-in needed")
    except Exception as exc:
        return record_failure(st, "check", f"{type(exc).__name__}: {exc}"[:400], facts, crash=traceback.format_exc())


def do_implement(key):
    lock = open(lock_file(key), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return load(key)
    st = load(key)
    slot = take_slot("implement")
    if not slot:
        return st if st.get("phase") == "approved" else set_phase(st, "approved")
    fix = st.pop("fix_request", None)
    conflict = st.get("fix_conflict") and not fix
    rework = None if (fix or conflict) else st.pop("rework_request", None)
    resume = st.get("implement_session") if fix and st.get("run_id") else None
    run_id = st["run_id"] if resume else time.strftime("%Y%m%d-%H%M%S")
    set_phase(st, "implementing", error=None, started=now(), run_id=run_id)
    if fix and st.get("rework_mode"):
        # "I resolved it, check the latest": read the PR branch from GitHub again before the fix runs
        errs = prepare_rework(st)
        if errs:
            return record_failure(st, "implement", "\n".join(errs), errors=errs, rework=True)
        save(st)
    if conflict:
        errs = prepare_conflict_fix(st)
        if errs:
            return record_failure(st, "implement", "\n".join(errs), errors=errs, rework=True)
    if rework:
        st["rework_last"] = rework
        if not rework_new_branches() and any((p.get("live") or {}).get("state") in ("open", "draft", None) for p in st.get("prs") or []):
            errs = prepare_rework(st)
            if errs:
                return record_failure(st, "implement", "\n".join(errs), errors=errs, rework=True)
            st["rework_mode"] = True
        else:
            # a follow-up PR on new branches (stacked on an earlier PR that is still open), the old ones stay as history
            start_round(st)
        save(st)
    facts = {}
    try:
        d = prepare_ticket(key, st)
        manifest = RUNS / key / run_id / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        task = f"/ticket-work implement {key} {d} {manifest}"
        # a fix during a rework stays a rework: same PRs, same branches
        redo = rework or (st.get("rework_last") or {"instructions": ""} if (fix and st.get("rework_mode")) or conflict else None)
        if redo:
            write_rework(d, st, redo)
            task = f"/ticket-work rework {key} {d} {manifest}"
        if conflict:
            task = conflict_task(key, d, manifest, st)
        if resume:
            where = "".join(f"\n- {p['repo']}: branch `{p['branch']}`, worktree `{p['worktree']}`"
                            for p in st.get("prs") or [] if st.get("rework_mode") and p.get("worktree"))
            prompt = fix_prompt(st, fix, "implementation") + (github_changes_text(st) + "\n" if github_changes_text(st) else "") + (
                (f"Work only in these worktrees (they match GitHub now), not in any path from earlier:{where}\n\n" if where else "")
                + "Keep the same worktrees and branches, commit the fix there, never push, then finish the "
                f"/ticket-work {'rework' if redo else 'implement'} procedure and write the manifest to {manifest} again (overwrite it).")
        else:
            prompt = task + (f"\n\nNote from the developer after the last attempt failed: {fix['instructions']}" if fix else "")
        wts = [Path(p["worktree"]) for p in st.get("prs") or [] if st.get("rework_mode") and p.get("worktree")]
        res, text, facts = claude_run(f"{prompt}\n\n{UNATTENDED}", CFG["implement_budget_usd"], CFG["implement_timeout_minutes"],
                                      LOGS / f"{key}-implement-{run_id}-{int(time.time())}.json", [d, manifest.parent] + wts,
                                      "implement", st, resume=resume)
        st["cost_implement"] = (st.get("cost_implement") or 0) + (res.get("total_cost_usd") or 0)
        st["implement_session"] = facts["session_id"] or st.get("implement_session")
        if not manifest.exists():
            return record_failure(st, "implement", f"the run wrote no manifest: {text[-300:]}", facts)
        m = json.loads(manifest.read_text())
        st["manifest"] = str(manifest)
        st["implement_summary"] = m.get("summary")
        if m.get("verdict") == "needs_context":
            a = st.get("assessment") or {}
            a["questions"] = m.get("questions") or a.get("questions") or []
            st["assessment"] = a
            notify(f"{key} needs your input", "The implementation stopped to ask before writing code.")
            return set_phase(st, "needs_context")
        if m.get("verdict") != "done":
            return record_failure(st, "implement", m.get("summary") or "blocked", {**facts, "blocked": True})
        st.update({"failure": None, "diagnosis": None})
        # save first: finalize reloads the state from disk
        save(st)
        return finalize(key)
    except SignInNeeded as exc:
        tracker_status(False, error=str(exc))
        return set_phase(st, "approved", error=f"{TR.name} sign-in needed, it starts again once you sign in")
    except Exception as exc:
        return record_failure(st, "implement", f"{type(exc).__name__}: {exc}"[:400], facts, crash=traceback.format_exc())


# ---------- failures: what happened, why, and how to fix it ----------

PR_CHECK_HINTS = [
    (r"isn't authored and committed by", "A commit has the wrong author or committer. The agent only pushes commits made as you, so the commit needs re-authoring (`git commit --amend --reset-author`)."),
    (r"doesn't follow", "The branch name doesn't match your format, so it was refused before pushing. The branch needs renaming to the configured format."),
    (r"title must follow", "The PR title doesn't match your title format. The title in the manifest needs rewriting."),
    (r"message doesn't mention", "A commit message doesn't name the ticket. The commit message needs the ticket number."),
    (r"has attribution", "A commit message mentions Claude or has a trailer. It needs rewording without it."),
    (r"missing the attribution trailer", "A commit was made without the trailer your attribution setting asks for. Commits "
     "made through the agent's git get it; this one needs `git commit --amend --no-edit --trailer` with it."),
    (r"uncommitted changes|files left out", "Some changes were left uncommitted in the worktree, so pushing would have dropped them."),
    (r"already exists on GitHub", "A branch with the same name already exists on GitHub. The agent never pushes over an existing branch, so it needs a new slug."),
    (r"base must be", "The PR would target the wrong base branch."),
    (r"secrets-looking file", "A file that looks like a secret (.env, key, certificate) is in the diff, so nothing was pushed."),
    (r"no commits on top", "The branch has no commits, so there's nothing to open a PR for."),
    (r"PR body: (bold text|markdown header|table|emoji|unclosed code fence|tool or AI attribution)",
     "The PR description breaks your style switches in references/policy.md (or has attribution), so it was refused "
     "before pushing. Re-running only the PR step can't change that: the body in the manifest needs rewriting."),
    (r"empty PR body", "The PR description is empty. The manifest needs a body."),
    (r"worktree .* doesn't exist|isn't on", "The worktree the manifest names is missing or on another branch."),
]


USAGE_LIMIT = re.compile(r"(session|usage|weekly|5-hour) limit|hit your limit", re.I)


def usage_reset_at(text):
    """When the Claude plan limit resets, from a message like 'resets 6pm', as a timestamp. None if not said."""
    m = re.search(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text or "", re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    h = h % 12 + (12 if ap == "pm" else 0) if ap else h
    t = time.localtime()
    at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, mi, 0, 0, 0, -1))
    return at + 86400 if at < time.time() - 60 else at


def explain(f):
    """A plain explanation from the facts alone, shown at once, before any diagnosis run."""
    step = {"check": "check", "implement": "implementation", "pr": "PR step"}.get(f.get("step"), "run")
    last = (f.get("last_steps") or [None])[-1]
    doing = f' The last thing it did was "{last}".' if last else ""
    text = f"{f.get('message') or ''} {f.get('result_text') or ''} {f.get('stderr') or ''}".lower()
    if f.get("stopped"):
        return {"title": f"The {step} stopped before it finished",
                "detail": "Its process ended without a result. That happens when the Mac sleeps or restarts, the agent is updated, "
                          f"or the process is killed.{doing} Its worktrees and commits are still there, so it can resume."}
    if f.get("timed_out"):
        return {"title": f"The {step} ran past its {f.get('timeout_min')}-minute limit",
                "detail": f"It was stopped at the time limit.{doing} A long test suite or a command that never returns is the usual cause."}
    if f.get("subtype") == "error_max_budget_usd" or (f.get("cost") and f.get("budget") and f["cost"] >= 0.97 * f["budget"]):
        return {"title": f"The {step} used its whole ${f.get('budget')} budget",
                "detail": f"It spent ${(f.get('cost') or 0):.2f} before finishing.{doing} Big tickets or repeated test runs use more. "
                          "Resuming continues with a fresh budget, or raise the cap in the config."}
    if f.get("subtype") == "error_max_turns":
        return {"title": f"The {step} hit the turn limit", "detail": f"It took more steps than allowed.{doing}"}
    # the PR checks' own errors first: their text holds commit ids, which can contain digits like 403
    if f.get("step") == "pr" and f.get("errors"):
        hints = []
        for e in f["errors"]:
            hint = next((h for rx, h in PR_CHECK_HINTS if re.search(rx, e)), None)
            if hint and hint not in hints:
                hints.append(hint)
        body = any(re.search(r"PR body", e) for e in f["errors"])
        return {"title": "The PR checks failed, so nothing was pushed",
                "detail": " ".join(hints) or "A check the agent runs before pushing failed. The errors are below.",
                "fix": ("Rewrite the PR body in the manifest so it passes the style switches in "
                        "~/.claude/skills/pr-review/references/policy.md. Change nothing else." if body else
                        "Fix what each error below says in the worktrees (amend the commits, never push), update the manifest.")}
    if USAGE_LIMIT.search(text):
        at = f.get("retry_at")
        when = time.strftime("%H:%M", time.localtime(at)) if at else "the limit resets"
        return {"title": "You hit your Claude usage limit",
                "detail": f"Claude stopped the {step} with: \"{(f.get('result_text') or f.get('message') or '').strip()[:160]}\". "
                          f"Nothing is wrong with the ticket. It resumes on its own at {when}, in the same session, or press Fix it then."}
    if "not logged in" in text or re.search(r"\b401\b", text) or "invalid api key" in text:
        return {"title": "Claude couldn't sign in", "detail": "The headless run had no valid credentials. Run `ptk doctor` in a terminal."}
    if "not allowed to access model" in text or re.search(r"\b403\b", text):
        return {"title": f"The model {f.get('model')} isn't allowed on this account",
                "detail": "Pick another model for this ticket, or run Check which models work in Run settings."}
    if "rate limit" in text or "overloaded" in text or re.search(r"\b529\b", text):
        return {"title": "Claude was overloaded or rate limited", "detail": f"The API refused requests for a while.{doing} Running it again usually works."}
    if f.get("blocked"):
        return {"title": f"The {step} stopped because it was blocked", "detail": f.get("message") or ""}
    if f.get("crash"):
        return {"title": f"The ticket agent's own code failed during the {step}",
                "detail": f"{f.get('message')}. This is a bug in the agent, not in your ticket. The full traceback is below."}
    if "wrote no" in text:
        return {"title": f"The {step} finished but wrote no result file",
                "detail": f"It ended without writing the file the agent reads.{doing} Its last message is below."}
    return {"title": f"The {step} failed", "detail": (f.get("message") or "")[:400]}


def record_failure(st, step, message, facts=None, phase="failed", crash=None, **extra):
    """Keep everything needed to explain and fix a failed step, then start a diagnosis in the background."""
    f = {"id": f"{step}-{int(time.time())}", "step": step, "message": message, "at": now(), **(facts or {}), **extra}
    if crash:
        f["crash"] = crash[-3000:]
    limited = bool(USAGE_LIMIT.search(f"{message} {f.get('result_text') or ''}"))
    if limited:
        f["retry_at"] = (usage_reset_at(f"{message} {f.get('result_text') or ''}") or time.time() + 3600) + 120
    f["explained"] = explain(f)
    st.update({"failure": f, "diagnosis": None})
    set_phase(st, phase, error=message, failed_step=step)
    notify(f"{st['key']}: {f['explained']['title']}", "The ticket desk explains why and how to fix it")
    # a usage limit needs no diagnosis (it would hit the same limit), it resumes after the reset
    if CFG.get("diagnose", True) and not limited and not extra.get("quiet"):
        spawn("_diagnose", st["key"])
    return st


def transcript_path(session_id):
    """Where Claude Code keeps the session's transcript for runs started in the workspace."""
    if not session_id:
        return None
    f = HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(WS)) / f"{session_id}.jsonl"
    return f if f.exists() else None


DIAGNOSE_PROMPT = """A run of the ticket agent failed on ticket {key}. Find out what went wrong and why, and how to fix it.
This is read-only: don't edit files, don't commit, don't push, don't change branches. Look, run read-only commands, and
re-run a failing test or command when that proves the cause.

Facts the agent recorded (JSON): {facts}

Where to look:
- the run's transcript (JSONL, read the end first): {transcript}
- the run log: {log}
- the ticket folder (ticket.md, conventions.md, assessment.json): {ticket_dir}
- the worktrees of this ticket: {worktrees}
- the agent's log lines for {key}: {agent_log}

Write the developer a plain, specific explanation in simple English: what happened, the root cause with the evidence
(path:line, a log line or a command you ran and its output), and the smallest fix. Then write this JSON to {out}:
{{"summary": "one sentence a developer gets at a glance",
  "what_happened": "2-4 sentences, in order, what the run did and where it stopped",
  "why": "the root cause, 1-3 sentences",
  "evidence": ["path:line, log line or command plus output that proves it", "..."],
  "fix": {{"action": "resume" | "rerun" | "rerun_pr_step" | "needs_you",
           "instructions": "what the agent should do when it resumes, concrete and short",
           "needs_from_you": "only when the developer must decide or provide something, else empty"}},
  "confidence": "high" | "medium" | "low"}}
"resume" continues the same session with the instructions, "rerun" starts the step again from scratch,
"rerun_pr_step" only re-runs the push and PR checks, "needs_you" means it can't be fixed without the developer.
End with the line RESULT: written."""


def do_diagnose(key):
    lock = open(lock_file(f"{key}.diagnose"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    slot = take_slot("diagnose")
    while not slot:
        time.sleep(20)
        slot = take_slot("diagnose")
    st = load(key) or {}
    f = st.get("failure")
    if not f:
        return
    fid = f["id"]

    def update(**fields):
        # a fix may have started meanwhile: only touch this failure's diagnosis, on the newest state
        cur = load(key) or {}
        if (cur.get("failure") or {}).get("id") != fid:
            return False
        cur.update(fields)
        save(cur)
        return True
    update(diagnosis={"state": "running", "started": now()})
    d = TICKETS / key
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"diagnosis-{fid}.json"
    out.unlink(missing_ok=True)
    wts = sorted({str(Path(pr["worktree"])) for pr in (st.get("prs") or []) if pr.get("worktree")}) \
        or [str(w) for w in sorted((WS / ".local" / "wt").glob(f"*-{key.lower()}"))]
    tr = transcript_path(f.get("session_id"))
    facts = {k: v for k, v in f.items() if k not in ("explained",)}
    prompt = DIAGNOSE_PROMPT.format(key=key, facts=json.dumps(facts)[:6000], transcript=tr or "not found",
                                    log=f.get("log") or "none", ticket_dir=d, worktrees=", ".join(wts) or "none yet",
                                    agent_log=LOGS / "agent.log", out=out)
    dirs = [d, LOGS] + [Path(w) for w in wts] + ([tr.parent] if tr else [])
    try:
        res, text, facts = claude_run(prompt, CFG.get("diagnose_budget_usd", 2), CFG.get("diagnose_timeout_minutes", 15),
                                      LOGS / f"{key}-diagnose-{int(time.time())}.json", dirs, "diagnose", st, read_only=True)
        cost = res.get("total_cost_usd") or 0
        cur = load(key) or {}
        cur["cost_diagnose"] = (cur.get("cost_diagnose") or 0) + cost
        save(cur)
        if not out.exists():
            update(diagnosis={"state": "failed", "error": (text or "it wrote no diagnosis")[-400:], "cost": cost})
            return
        diag = json.loads(out.read_text())
        update(diagnosis={**diag, "state": "done", "at": now(), "cost": cost,
                          "model": facts.get("model"), "effort": facts.get("effort")})
        log(f"{key} diagnosed: {diag.get('summary', '')[:120]}")
    except Exception as exc:
        update(diagnosis={"state": "failed", "error": f"{type(exc).__name__}: {exc}"[:400]})


def request_fix(key, instructions, action="resume"):
    """Queue the failed step again: resume its session with the fix, start it fresh, or re-run only the PR step."""
    st = known(key)
    if st.get("phase") not in ("failed", "needs_attention"):
        raise SystemExit(f"{key} isn't failed, it's {st.get('phase')}")
    step = st.get("failed_step") or "implement"
    if action == "rerun_pr_step" or (step == "pr" and action != "resume"):
        spawn("_repush" if st.get("rework_mode") else "finalize", key)
        return "Running the PR step again."
    fix = {"instructions": (instructions or "").strip() or "Fix the cause described above and finish.", "at": now(), "action": action}
    if action == "resume":
        st["fix_request"] = fix
        touch_user(st)
    elif st.get("rework_mode") or (st.get("failure") or {}).get("rework") or in_followup(st):
        st["rework_request"] = {**(st.get("rework_last") or {}), "at": now()}
        if (instructions or "").strip():
            st["rework_request"]["instructions"] = f"{st['rework_request'].get('instructions') or ''}\n\n{instructions.strip()}".strip()
        st["rework_mode"] = False
        touch_user(st)
    elif fix["instructions"]:
        st.setdefault("context", []).append({"at": now(), "text": f"Note after a failed {step}: {fix['instructions']}"})
        touch_user(st)
    job = "check" if step == "check" else "implement"
    set_phase(st, QUEUED[job], error=None)
    start_queued()
    return ("Fixing it in the same session." if action == "resume" else "Starting it again from scratch.") + \
        ("" if parallel_limit(job) - running_count(job) > 0 else " It starts when a slot frees up.")


# ---------- PR step: deterministic checks, then push and open draft PRs ----------

def validate(st, pr):
    """Every reason this PR must not be pushed. Empty means it's safe."""
    errs = []
    key = st["key"]
    repo = pr.get("repo", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo) or (CFG.get("repos") and repo not in CFG["repos"]):
        return [f"{repo}: not one of the repos this agent may open PRs in"]
    try:
        rdir = repo_dir(repo)
    except Exception as exc:
        return [f"{repo}: {exc}"]
    origin = git("-C", str(rdir), "remote", "get-url", "origin")
    if not re.search(rf"{re.escape(HOST)}[:/]{re.escape(ORG)}/{re.escape(repo)}(\.git)?/?$", origin):
        return [f"{repo}: origin {origin} isn't {HOST}:{ORG}/{repo}"]
    wt = Path(pr.get("worktree", ""))
    if not (wt / ".git").is_file():
        return [f"{repo}: worktree {wt} doesn't exist"]
    common = Path(git("-C", str(wt), "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    if common.resolve() != rdir.resolve():
        errs.append(f"{repo}: worktree belongs to {common}, not {rdir}")
    branch = pr.get("branch", "")
    if not re.fullmatch(branch_re(key), branch):
        errs.append(f"{repo}: branch '{branch}' doesn't follow {branch_name(key)}")
    if git("-C", str(wt), "branch", "--show-current") != branch:
        errs.append(f"{repo}: worktree isn't on {branch}")
    default = default_branch(rdir, repo)
    base = pr.get("base", "")
    # a follow-up round may be based on an earlier PR of this ticket that is still open, and nothing else
    stack = {p["branch"] for p in st.get("previous_prs") or [] if p.get("repo") == repo and p.get("branch") and open_pr(p)}
    if base != default and base not in stack:
        errs.append(f"{repo}: base must be {default}" + (f" or the open earlier PR branch {', '.join(sorted(stack))}" if stack else "")
                    + f", got '{base}'")
        base = default
    try:
        git("-C", str(rdir), "fetch", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    except RuntimeError:
        return errs + [f"{repo}: the base branch {base} is gone from GitHub (its PR was probably merged meanwhile). "
                       f"Rebuild the branch on {default} and run the PR step again"]
    if run([REAL_GIT, "-C", str(wt), "merge-base", "--is-ancestor", f"origin/{base}", "HEAD"], check=False).returncode != 0:
        pr["behind"] = int(git("-C", str(wt), "rev-list", "--count", f"HEAD..origin/{base}") or 0)
    commits = git("-C", str(wt), "log", "--format=%H%x00%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e", f"origin/{base}..HEAD")
    entries = [c for c in commits.split("\x1e") if c.strip()]
    if not entries:
        errs.append(f"{repo}: no commits on top of {base}")
    a = CFG["author"]
    names = TR.mentions(st)
    for c in entries:
        sha, parents, an, ae, cn, ce, msg = c.strip("\n").split("\x00", 6)
        if (an, ae.lower()) != (a["name"], a["email"].lower()) or (cn, ce.lower()) != (a["name"], a["email"].lower()):
            errs.append(f"{repo}: commit {sha[:7]} isn't authored and committed by {a['name']} <{a['email']}>")
        if not any(re.search(re.escape(n) + r"(?!\d)", msg) for n in names):
            errs.append(f"{repo}: commit {sha[:7]} message doesn't mention {key}")
        bad = attribution_error(repo, sha, msg, merge=len(parents.split()) > 1)
        if bad:
            errs.append(bad)
    if git("-C", str(wt), "status", "--porcelain", "--untracked-files=no"):
        errs.append(f"{repo}: uncommitted changes in the worktree")
    untracked = [l[3:] for l in git("-C", str(wt), "status", "--porcelain").splitlines() if l.startswith("??")]
    untracked = [u for u in untracked if not NOISE.search(u)]
    if untracked:
        errs.append(f"{repo}: files left out of the commit: {', '.join(untracked[:5])}")
    for f in git("-C", str(wt), "diff", "--name-only", f"origin/{base}...HEAD").splitlines():
        if SENSITIVE.search(f):
            errs.append(f"{repo}: refuses to push a secrets-looking file: {f}")
    if run([REAL_GIT, "-C", str(rdir), "ls-remote", "--exit-code", "--heads", "origin", branch], check=False).returncode == 0:
        if not pr.get("pushed"):
            errs.append(f"{repo}: branch {branch} already exists on GitHub")
    title = pr.get("title", "")
    if not re.fullmatch(title_re(key), title):
        errs.append(f"{repo}: title must follow '{CONV['pr_title']}', got '{title}'")
    sys.path.insert(0, str(REVIEW_SKILL / "scripts"))
    from post_review import lint_text
    e, w = [], []
    template = pr_template(repo, rdir)
    lint_text(f"{repo} PR body", pr.get("body", ""), e, w, inline=False, structure=not template)
    errs += e
    if template and (pr.get("body") or "").strip():
        errs += template_errors(repo, pr.get("body", ""), template[1])
    if not (pr.get("body") or "").strip():
        errs.append(f"{repo}: empty PR body")
    return errs


def finalize(key, dry_run=False):
    st = load(key)
    if not st or not st.get("manifest"):
        raise RuntimeError(f"{key} has no implementation to open PRs for")
    if st.get("rework_mode") and not dry_run:
        return finalize_rework(key)
    m = json.loads(Path(st["manifest"]).read_text())
    prs = sorted(m.get("prs") or [], key=lambda p: p.get("order", 99))
    if not prs:
        return set_phase(st, "failed", error="the manifest lists no PRs", failed_step="pr")
    # a retry keeps what already happened: pushed branches and opened PRs
    prev = {p["repo"]: p for p in st.get("prs") or []}
    errors = []
    for pr in prs:
        pr.update({k: prev[pr["repo"]][k] for k in ("pushed", "url", "number") if k in prev.get(pr["repo"], {})})
        if pr.get("url"):
            continue
        errors += validate(st, pr)
    if m.get("replies"):
        sys.path.insert(0, str(REVIEW_SKILL / "scripts"))
        from post_review import lint_text  # noqa: E402
        errors += reply_errors(st, m, lint_text)
    if errors:
        st["prs"] = prs
        return record_failure(st, "pr", "\n".join(errors), phase="needs_attention", errors=errors)
    if dry_run:
        print("checks passed, would push and open:")
        for pr in prs:
            print(f"  {pr['repo']} {pr['branch']} -> {pr['base']}  {pr['title']}")
        return {**st, "error": None, "dry_run_ok": True}
    issue_line = TR.issue_line(st)
    body_of = lambda pr, extra="": with_footer(pr["body"].strip() + (f"\n\n{issue_line}" if issue_line and not
                                                                     links_ticket(st, pr["body"]) else "") + extra) + "\n"
    try:
        for pr in prs:
            if pr.get("url"):
                continue
            wt = pr["worktree"]
            if not pr.get("pushed"):
                git("-C", wt, "push", "--quiet", "-u", "origin", pr["branch"])
                pr["pushed"] = True
            body = RUNS / key / st["run_id"] / f"{pr['repo']}-body.md"
            body.write_text(body_of(pr))
            cmd = [REAL_GH, "pr", "create", "--repo", f"{ORG}/{pr['repo']}", "--base", pr["base"], "--head", pr["branch"],
                   "--title", pr["title"], "--body-file", str(body)] + (["--draft"] if CFG["draft_prs"] else [])
            url = run(cmd).stdout.strip().splitlines()[-1]
            pr["url"], pr["number"] = url, int(url.rstrip("/").split("/")[-1])
            pr["feedback_since"], pr["pushed_sha"] = utc_iso(), git("-C", wt, "rev-parse", "HEAD")
            st["prs"] = prs
            save(st)
        if len(prs) > 1:
            names = lambda p: f"{p['repo']} #{p['number']}"
            order = ", then ".join(names(p) for p in prs)
            for pr in prs:
                others = ", ".join(names(p) for p in prs if p is not pr)
                body = RUNS / key / st["run_id"] / f"{pr['repo']}-body.md"
                body.write_text(body_of(pr, f"\n\nPart of {key}, with {others}. Merge order: {order}."))
                run([REAL_GH, "pr", "edit", str(pr["number"]), "--repo", f"{ORG}/{pr['repo']}", "--body-file", str(body)])
    except Exception as exc:
        st["prs"] = prs
        return record_failure(st, "pr", f"{type(exc).__name__}: {exc}"[:400], phase="needs_attention", errors=[str(exc)[:400]])
    st["prs"] = prs
    notify(f"Draft PRs opened for {key}", ", ".join(f"{p['repo']} #{p['number']}" for p in prs))
    if m.get("replies"):
        st["prs"] = prs
        replied, unreplied = post_replies(st, m, {p["repo"]: p.get("url") for p in prs})
        st.setdefault("reworks", []).append({"at": now(), "instructions": (st.get("rework_last") or {}).get("instructions"),
                                             "summary": m.get("summary"), "prs": {p["repo"]: {"commits": 0, "changes": "follow-up PR"}
                                                                                  for p in prs},
                                             "replies": replied, "replies_failed": unreplied})
    st.update({"failure": None, "diagnosis": None})
    return set_phase(st, "pr_open", error=None, opened_at=now(), worked_at_ts=time.time(),
                     tracker_updated_at_pr=st.get("tracker_updated"))


# ---------- after the PRs open: their state on GitHub, feedback, and rework on the same PRs ----------

BOT = re.compile(r"(\[bot\]$|^github-actions$|^dependabot|^codecov|^sonarcloud|^copilot)", re.I)
PR_FIELDS = "state,isDraft,reviewDecision,mergedAt,headRefOid,updatedAt,statusCheckRollup,comments,reviews,title,url,mergeable,mergeStateStatus"
_ME = {}


def utc_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def gh_json(*args, timeout=60):
    out = run([REAL_GH, *args], timeout=timeout).stdout
    return json.loads(out) if out.strip() else None


def me_login():
    if "login" not in _ME:
        _ME["login"] = (gh_json("api", "user") or {}).get("login")
    return _ME["login"]


def check_summary(rollup):
    c = {"passed": 0, "failed": 0, "pending": 0, "skipped": 0}
    for x in rollup or []:
        v = (x.get("conclusion") or x.get("state") or "").upper()
        if v in ("SUCCESS", "NEUTRAL"):
            c["passed"] += 1
        elif v == "SKIPPED":
            c["skipped"] += 1
        elif v in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"):
            c["failed"] += 1
        else:
            c["pending"] += 1
    return c


def comment_id(c):
    """A PR conversation comment's id, the number GitHub shows in its link."""
    m = re.search(r"issuecomment-(\d+)", c.get("url") or "")
    return f"comment-{m.group(1) if m else c.get('id')}"


def feedback_lines(prs):
    """The comments on these PRs for rework.md, new first, each with the id a reply answers."""
    lines = []
    for p in prs:
        fb = (p.get("live") or {}).get("feedback") or []
        for f in sorted(fb, key=lambda x: (not x["new"], x["at"] or "")):
            where = f" on {f['where']}" if f.get("where") else ""
            tag = "NEW " if f["new"] else ""
            answered = " (you replied already)" if f.get("id") in (p.get("replied") or {}) else ""
            to = f" [reply id: {f['id']}]" if f.get("id") and not f["bot"] else ""
            lines.append(f"- {tag}{p['repo']} #{p['number']} {f['kind']} by {f['who']}{' (bot)' if f['bot'] else ''}{where}{to}"
                         f"{answered}, {f['at']}: {f['body'].strip()[:1200]}")
        if not fb:
            lines.append(f"- {p['repo']} #{p['number']}: no comments")
    return lines


REPLY_HOWTO = ("Answer each comment you act on, or decide not to act on, in the manifest's `replies`: "
               "`{\"repo\": \"<repo>\", \"pr\": <number>, \"to\": \"<reply id above>\", \"body\": \"<your reply>\"}`. "
               "The agent posts them in place after the push: in the thread for inline comments, quoting the comment for the "
               "others. Never answer a comment in the PR description; change the description only when the change itself did.")


def pr_live(pr):
    """One PR as GitHub has it now, with what others said on it. 'new' marks what came after the last push."""
    repo, since = f"{ORG}/{pr['repo']}", pr.get("feedback_since") or ""
    j = gh_json("pr", "view", str(pr["number"]), "--repo", repo, "--json", PR_FIELDS) or {}
    inline = [c for page in (gh_json("api", f"repos/{repo}/pulls/{pr['number']}/comments", "--paginate", "--slurp") or [])
              for c in page]
    # every comment gets an id a rework can answer: inline ones in their thread, the others with a quoting reply
    items = [{"kind": "comment", "who": (c.get("author") or {}).get("login"), "at": c.get("createdAt"),
              "body": c.get("body") or "", "url": c.get("url"),
              "id": comment_id(c)} for c in j.get("comments") or []]
    items += [{"kind": {"CHANGES_REQUESTED": "requested changes", "APPROVED": "approved"}.get(r.get("state"), "reviewed"),
               "who": (r.get("author") or {}).get("login"), "at": r.get("submittedAt"), "body": r.get("body") or "", "url": None,
               "id": f"review-{r.get('id')}"}
              for r in j.get("reviews") or [] if (r.get("body") or "").strip() or r.get("state") in ("CHANGES_REQUESTED", "APPROVED")]
    items += [{"kind": "inline", "who": (c.get("user") or {}).get("login"), "at": c.get("created_at"), "body": c.get("body") or "",
               "url": c.get("html_url"), "where": f"{c.get('path')}:{c.get('line') or c.get('original_line') or ''}",
               "id": f"inline-{c.get('id')}", "thread": c.get("in_reply_to_id") or c.get("id")}
              for c in inline]
    me = me_login()
    feedback = []
    for it in sorted(items, key=lambda x: x["at"] or ""):
        if not it["who"] or it["who"] == me:
            continue
        it.update({"bot": bool(BOT.search(it["who"])), "new": (it["at"] or "") > since, "body": it["body"][:1500]})
        feedback.append(it)
    state = "merged" if j.get("mergedAt") else "closed" if j.get("state") == "CLOSED" else "draft" if j.get("isDraft") else "open"
    # GitHub's own answer: CONFLICTING is a merge conflict with the base branch, DIRTY the same in the merge status
    conflicts = j.get("mergeable") == "CONFLICTING" or j.get("mergeStateStatus") == "DIRTY"
    return {"state": state, "conflicts": bool(conflicts) and state in ("open", "draft"),
            "merge_state": j.get("mergeStateStatus"), "review": (j.get("reviewDecision") or "").lower() or None,
            "checks": check_summary(j.get("statusCheckRollup")), "head": j.get("headRefOid"), "title": j.get("title"),
            "updated": j.get("updatedAt"), "feedback": feedback[-40:], "synced": now()}


PR_PHASES = ("pr_open", "merged", "pr_closed")
PR_FRESH_S = 120


def prs_stale(st, max_age=PR_FRESH_S):
    """True when a PR of this ticket was last read from GitHub more than max_age seconds ago."""
    return any(p.get("number") and time.time() - (p.get("checked_ts") or 0) > max_age for p in st.get("prs") or [])


def sync_ticket_prs(key):
    """Read one ticket's PRs from GitHub and apply them. The ticket's phase follows them: open, merged or closed.
    False when the ticket was left alone (no PRs, running, or not in a PR phase)."""
    st = load(key)
    prs = [p for p in (st or {}).get("prs") or [] if p.get("number")]
    if not prs or is_running(key) or st.get("phase") not in PR_PHASES:
        return False
    read = {}
    for pr in prs:
        try:
            read[(pr["repo"], pr["number"])] = (pr_live(pr), None)
        except Exception as exc:
            read[(pr["repo"], pr["number"])] = (None, f"{type(exc).__name__}: {str(exc)[:200]}")
    # reload before writing: a click or a run may have changed the ticket while GitHub was being read
    st = load(key)
    if is_running(key) or st.get("phase") not in PR_PHASES:
        return False
    prs = [p for p in st.get("prs") or [] if (p.get("repo"), p.get("number")) in read]
    before_new = st.get("new_feedback") or 0
    for pr in prs:
        live, err = read[(pr["repo"], pr["number"])]
        if live:
            pr["live"] = live
            pr.pop("live_error", None)
        else:
            pr["live_error"] = err
        pr["checked_ts"] = time.time()
    states = [(pr.get("live") or {}).get("state") for pr in prs]
    st["new_feedback"] = sum(1 for pr in prs for f in (pr.get("live") or {}).get("feedback", []) if f["new"] and not f["bot"])
    phase = st["phase"]
    if states and all(x == "merged" for x in states):
        phase = "merged"
    elif states and all(x in ("merged", "closed") for x in states):
        phase = "pr_closed"
    elif any(x in ("open", "draft") for x in states):
        phase = "pr_open"
    if phase != st["phase"]:
        notify(f"{st['key']}: {'merged' if phase == 'merged' else 'PRs closed' if phase == 'pr_closed' else 'PRs open again'}",
               ", ".join(f"{p['repo']} #{p['number']}" for p in prs))
    if st["new_feedback"] > before_new:
        notify(f"New feedback on {st['key']}", f"{st['new_feedback']} new comments on its PR. Rework it from the ticket desk.")
    set_phase(st, phase)
    return True


def sync_prs():
    """Every ticket's PRs as they are on GitHub, read-only. The cycle runs this, and the desk's refresh button."""
    n = 0
    for st in all_states():
        n += bool(sync_ticket_prs(st["key"]))
    return n


def to_epoch(ts):
    """An ISO timestamp from GitHub ('...Z') or Jira ('...+0000', with milliseconds) as seconds, 0 when unreadable."""
    ts = re.sub(r"\.\d+", "", ts or "")
    ts = re.sub(r"Z$", "+0000", ts)
    ts = re.sub(r"([+-]\d\d):(\d\d)$", r"\1\2", ts)
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - (
            (int(ts[19:22]) * 3600 + (int(ts[19] + ts[22:24]) * 60 if len(ts) >= 24 else 0)) if len(ts) >= 24 else 0)
    except (ValueError, IndexError):
        return 0


def comments_since(d, since_ts):
    """Ticket comments written after since_ts, from the ticket folder's issue.json, whatever the tracker."""
    f = Path(d) / "issue.json"
    if not since_ts or not f.exists():
        return []
    try:
        comments = json.loads(f.read_text()).get("comments") or []
    except ValueError:
        return []
    return [c for c in comments if to_epoch(c.get("created")) > since_ts]


def rework_new_branches():
    """ticket.rework_branch: "same" adds commits to the PR's own branch while it's open (the default), "new" always
    opens a follow-up PR on a new branch, stacked on the earlier PR while it's open."""
    return CFG.get("rework_branch") == "new"


def open_pr(p):
    return (p.get("live") or {}).get("state") in ("open", "draft")


def in_followup(st):
    """A follow-up round was started and its PRs haven't opened yet (a retry must not start another round)."""
    return (st.get("round") or 1) > 1 and not any(p.get("url") for p in st.get("prs") or []) and bool(st.get("rework_last"))


def start_round(st):
    """Move the opened PRs to history and start the next round. A retry of a round that has no PRs yet changes nothing."""
    opened = [p for p in st.get("prs") or [] if p.get("url")]
    if not opened:
        st["rework_mode"] = False
        st["round"] = st.get("round") or 1
        return st
    rnd = st.get("round") or 1
    st["previous_prs"] = (st.get("previous_prs") or []) + [{**p, "round": rnd} for p in opened]
    st.update({"round": rnd + 1, "prs": [], "manifest": None, "implement_summary": None, "rework_mode": False,
               "error": None, "failed_step": None})
    log(f"{st['key']} round {st['round']} started")
    return st


IMAGE_MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"))
MAX_IMAGE_BYTES = 12_000_000


def save_attachment(key, name, raw):
    """A screenshot the user adds for the next rework, kept with the ticket's attachments. Images only, by content
    (a renamed text file or an SVG is refused). Returns the saved file name."""
    webp = raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    ext = next((e for magic, e in IMAGE_MAGIC if raw.startswith(magic)), ".webp" if webp else None)
    if not ext:
        raise SystemExit("only PNG, JPEG, GIF or WebP images can be attached")
    if len(raw) > MAX_IMAGE_BYTES:
        raise SystemExit("images can be up to 12 MB")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem)[:40].strip("-") or "image"
    target = TICKETS / check_key(key) / "attachments" / f"from-you-{time.strftime('%Y%m%d-%H%M%S')}-{stem}{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while target.exists():
        n += 1
        target = target.with_name(f"{target.stem.split('~')[0]}~{n}{ext}")
    target.write_bytes(raw)
    return target.name


def request_rework(key, instructions, images=()):
    """Queue more work on a ticket that already has PRs: the same PRs while they're open, a follow-up PR when merged."""
    st = known(key)
    if is_running(key):
        raise SystemExit(f"{key} is running now, rework it when it finishes")
    if not [p for p in st.get("prs") or [] if p.get("number")]:
        raise SystemExit(f"{key} has no PRs yet, check or approve it instead")
    if rework_new_branches():
        # the new branch starts from the earlier PR (open) or the default branch (merged), so read GitHub first
        try:
            sync_ticket_prs(key)
        except Exception as exc:
            raise SystemExit(f"couldn't read {key}'s PRs from GitHub: {type(exc).__name__}: {str(exc)[:200]}")
        st = known(key)
        unread = [p for p in st.get("prs") or [] if p.get("number") and not (p.get("live") or {}).get("state")]
        if unread:
            raise SystemExit(f"couldn't read {', '.join('#' + str(p['number']) for p in unread)} from GitHub "
                             f"({unread[0].get('live_error') or 'no answer'}), so it can't tell where the new branch starts. Try again")
    shots = [Path(n).name for n in images if (TICKETS / key / "attachments" / Path(n).name).is_file()]
    st["rework_request"] = {"instructions": (instructions or "").strip(), "images": shots, "at": now()}
    touch_user(st)
    set_phase(st, "approved", error=None)
    start_queued()
    free = parallel_limit("implement") - running_count("implement") > 0
    what = "Follow-up PR" if rework_new_branches() else "Rework"
    return f"{what} started." if free else f"{what} queued, it starts when an implementation slot frees up."


def link_pr(key, repo, number):
    """Make an open PR of the person's own (one they opened by hand) this ticket's PR, so reworks add commits to its
    branch, never force-pushed. A teammate's PR, a fork or a closed PR is refused."""
    st = known(key)
    if is_running(key):
        raise SystemExit(f"{key} is running now, link the PR when it finishes")
    repo = str(repo).split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo) or (CFG.get("repos") and repo not in CFG["repos"]):
        raise SystemExit(f"{repo} isn't one of the repos this agent works in")
    for p in st.get("prs") or []:
        if p.get("repo") == repo and p.get("number") == number:
            return f"#{number} is already this ticket's PR"
    j = gh_json("pr", "view", str(number), "--repo", f"{ORG}/{repo}", "--json",
                "number,state,author,isCrossRepository,headRefName,baseRefName,headRefOid,title,body,url") or {}
    if not j:
        raise SystemExit(f"couldn't read {ORG}/{repo}#{number} from GitHub")
    if j.get("state") != "OPEN":
        raise SystemExit(f"#{number} is {str(j.get('state')).lower()}, only an open PR can take more commits")
    if j.get("isCrossRepository"):
        raise SystemExit(f"#{number} comes from a fork, the agent only pushes to {ORG}/{repo}")
    author, me = (j.get("author") or {}).get("login"), me_login()
    if not me or author != me:
        raise SystemExit(f"#{number} was opened by {author}, not you ({me}). The agent only adds commits to your own PRs")
    same = [p for p in st.get("prs") or [] if p.get("repo") == repo]
    if any(open_pr(p) or not (p.get("live") or {}).get("state") for p in same if p.get("number")):
        raise SystemExit(f"{key} already has an open PR in {repo}, rework that one or close it first")
    # earlier closed or merged PRs in that repo stay as history
    st["previous_prs"] = (st.get("previous_prs") or []) + [{**p, "round": st.get("round") or 1} for p in same if p.get("url")]
    st["prs"] = [p for p in st.get("prs") or [] if p.get("repo") != repo] + [{
        "repo": repo, "number": number, "url": j["url"], "branch": j["headRefName"], "base": j["baseRefName"],
        "title": j["title"], "body": j.get("body") or "", "pushed": True, "pushed_sha": j["headRefOid"],
        "feedback_since": utc_iso(), "linked_at": now(), "order": len(st.get("prs") or []) + 1}]
    touch_user(st)
    # what an earlier run said no longer applies: say what the ticket has now
    set_phase(st, "pr_open", error=None, failure=None, diagnosis=None, failed_step=None, rework_mode=False,
              implement_summary=f"Linked #{number} ({j['headRefName']}), a PR you opened. Reworks add commits to it.")
    log(f"{key} linked {repo}#{number} ({j['headRefName']})")
    try:
        sync_ticket_prs(key)
    except Exception:
        pass
    return f"Linked #{number}, reworks add commits to {j['headRefName']}"


def conflict_task(key, d, manifest, st):
    """The run's job when a PR conflicts with its base: merge the base into the PR's own branch, resolve, test."""
    req = st.get("conflict_request") or {}
    lines = [f"/ticket-work rework {key} {d} {manifest}", "", "This is a merge conflict fix, not a new change:"]
    for p in st.get("prs") or []:
        if p.get("repo") in (req.get("repos") or []):
            base = p.get("base") or "the default branch"
            lines.append(f"- {p['repo']}: in worktree `{p.get('worktree')}` on branch `{p.get('branch')}`, run "
                         f"`git merge --no-edit origin/{base}`, resolve every conflict keeping what the PR is for, then run the "
                         "tests the PR touches. Commit the merge (not a squash, not a rebase). Never force, never another branch.")
    if (req.get("instructions") or "").strip():
        lines += ["", f"How to resolve it, from the developer: {req['instructions'].strip()}"]
    lines += ["", "Write the manifest as a rework does, with each PR's changes saying the merge and how the conflict was resolved."]
    return "\n".join(lines)


def request_conflict_fix(key, instructions=""):
    """Queue a fix for a merge conflict on this ticket's own open PR: the base merged into the PR's branch."""
    st = known(key)
    if is_running(key):
        raise SystemExit(f"{key} is running now, fix the conflict when it finishes")
    open_prs = [p for p in st.get("prs") or [] if p.get("number") and (p.get("live") or {}).get("state") in ("open", "draft")]
    if not open_prs:
        raise SystemExit(f"{key} has no open PR to fix")
    conflicted = [p for p in open_prs if (p.get("live") or {}).get("conflicts")]
    if not conflicted:
        raise SystemExit(f"GitHub doesn't show a conflict on {key}'s open PRs. Refresh the PR status first, or check it's still there")
    st["fix_conflict"] = True
    st["conflict_request"] = {"instructions": (instructions or "").strip(), "repos": [p["repo"] for p in conflicted], "at": now()}
    st.setdefault("rework_last", {"instructions": f"Fix the merge conflict with {conflicted[0].get('base') or 'the base branch'}."})
    st["rework_request"] = {"instructions": "", "images": [], "at": now(), "conflict": True}
    touch_user(st)
    set_phase(st, "approved", error=None)
    start_queued()
    return "Conflict fix started." if parallel_limit("implement") - running_count("implement") > 0 else "Conflict fix queued."


def prepare_conflict_fix(st):
    """Level the PR branches with GitHub (as a rework does), so the merge starts from what is on the branch. Returns errors."""
    errs = prepare_rework(st)
    if not errs:
        st["rework_mode"] = True
    save(st)
    return errs


def own_worktree(st, pr):
    """The agent's own worktree for a PR, always under <workspace>/.local/wt. A worktree recorded anywhere else (moved
    there by hand or by another session) is never used: the agent works in its own."""
    local = WS / ".local" / "wt"
    p = Path(pr.get("worktree") or "")
    if p.parent == local:
        return p
    rnd = st.get("round") or 1
    if pr.get("linked_at"):
        return local / f"{pr['repo']}-{st['key'].lower()}-pr{pr['number']}"
    return local / (f"{pr['repo']}-{st['key'].lower()}" + (f"-r{rnd}" if rnd > 1 else ""))


def checked_out_elsewhere(rdir, branch, mine):
    """Worktrees other than the agent's own that have this branch checked out."""
    out, path = [], None
    for line in git("-C", str(rdir), "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line == f"branch refs/heads/{branch}" and path and Path(path).resolve() != Path(mine).resolve():
            out.append(path)
    return out


def is_ancestor(wt, a, b):
    return run([REAL_GIT, "-C", str(wt), "merge-base", "--is-ancestor", a, b], check=False).returncode == 0


def prepare_rework(st):
    """Bring the agent's own worktree for each open PR level with its branch on GitHub, before every rework and every
    fix: what someone pushed (a conflict resolved by hand, a commit suggestion) is taken as it is, even a rewritten
    (force-pushed) branch when the agent has nothing unpushed. Records what changed on GitHub for the run. Returns errors."""
    errs = []
    for pr in st["prs"]:
        if (pr.get("live") or {}).get("state") not in ("open", "draft", None):
            continue
        rdir, branch = repo_dir(pr["repo"]), pr["branch"]
        wt = own_worktree(st, pr)
        try:
            git("-C", str(rdir), "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            remote = git("-C", str(rdir), "rev-parse", f"refs/remotes/origin/{branch}")
            if not (wt / ".git").exists():
                if checked_out_elsewhere(rdir, branch, wt):
                    # someone has the branch checked out in their own worktree: leave it alone, work detached and push by name
                    git("-C", str(rdir), "worktree", "add", "--quiet", "--detach", str(wt), remote)
                    pr["detached"] = True
                else:
                    has_local = run([REAL_GIT, "-C", str(rdir), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0
                    git("-C", str(rdir), "worktree", "add", "--quiet", *([str(wt), branch] if has_local else ["-b", branch, str(wt), remote]))
                    pr["detached"] = False
            pr["worktree"] = str(wt)
            current = git("-C", str(wt), "branch", "--show-current")
            if current != ("" if pr.get("detached") else branch):
                errs.append(f"{pr['repo']}: worktree {wt} isn't on {branch}")
                continue
            if git("-C", str(wt), "status", "--porcelain", "--untracked-files=no"):
                errs.append(f"{pr['repo']}: uncommitted changes in {wt}, commit or discard them first")
                continue
            local, last = git("-C", str(wt), "rev-parse", "HEAD"), pr.get("pushed_sha")
            follow = lambda: git("-C", str(wt), *(["checkout", "--quiet", "--detach", remote] if pr.get("detached")
                                                  else ["reset", "--quiet", "--hard", remote]))
            if local != remote:
                if is_ancestor(wt, local, remote):
                    follow()
                elif is_ancestor(wt, remote, local):
                    pass  # the agent's own commits on top, not pushed yet
                elif last and (local == last or is_ancestor(wt, local, last)):
                    follow()  # the branch was rewritten on GitHub and the agent has nothing of its own to keep
                else:
                    errs.append(f"{pr['repo']}: {branch} was rewritten on GitHub, and this worktree has commits that aren't "
                                "on it. Nothing was changed. Push them by hand or drop them, then rework again")
                    continue
            if last and last != remote:
                if is_ancestor(wt, last, remote):
                    log = git("-C", str(wt), "log", "--first-parent", "--format=%h %an: %s", f"{last}..{remote}").splitlines()
                    pr["github_changes"] = {"rewritten": False, "from": last[:9], "to": remote[:9], "commits": log[:20]}
                else:
                    pr["github_changes"] = {"rewritten": True, "from": last[:9], "to": remote[:9], "commits": []}
            else:
                pr.pop("github_changes", None)
            pr["rework_base"] = remote
            merge_base(rdir, wt, pr)
        except Exception as exc:
            errs.append(f"{pr['repo']}: {type(exc).__name__}: {str(exc)[:300]}")
    return errs


def merge_base(rdir, wt, pr):
    """Merge the latest base branch into the PR's branch before the rework, as the person, so the work starts from it.
    A conflict is left to the run, which must merge it before anything is pushed (ticket.rework_merge_base, default on)."""
    pr.pop("base_merge", None)
    pr.pop("base_tip", None)
    if not CFG.get("rework_merge_base", True):
        return
    base = pr.get("base") or default_branch(rdir, pr["repo"])
    git("-C", str(rdir), "fetch", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    tip = git("-C", str(rdir), "rev-parse", f"refs/remotes/origin/{base}")
    pr["base_tip"] = tip
    if is_ancestor(wt, tip, "HEAD"):
        return
    behind = int(git("-C", str(wt), "rev-list", "--count", f"HEAD..{tip}"))
    a = CFG["author"]
    out = run([REAL_GIT, "-C", str(wt), "-c", f"user.name={a['name']}", "-c", f"user.email={a['email']}",
               "merge", "--no-edit", f"refs/remotes/origin/{base}"], check=False)
    if out.returncode == 0:
        pr["base_merge"] = {"base": base, "behind": behind, "merged": True}
        return
    conflicts = git("-C", str(wt), "diff", "--name-only", "--diff-filter=U").splitlines()
    run([REAL_GIT, "-C", str(wt), "merge", "--abort"], check=False)
    pr["base_merge"] = {"base": base, "behind": behind, "merged": False, "conflicts": conflicts[:30],
                        "error": "" if conflicts else (out.stderr or out.stdout).strip()[-300:]}


def github_changes_text(st):
    """What happened on the PR branches on GitHub since the agent's last push, for the run's brief."""
    lines = []
    for p in st.get("prs") or []:
        g = p.get("github_changes")
        if not g:
            continue
        if g["rewritten"]:
            lines.append(f"- {p['repo']} #{p.get('number')}: the branch was rewritten (force-pushed) on GitHub, {g['from']} -> "
                         f"{g['to']}. Your worktree now matches GitHub. Treat it as the starting point.")
        else:
            lines.append(f"- {p['repo']} #{p.get('number')}: {len(g['commits'])} new commits on GitHub ({g['from']} -> {g['to']}), "
                         "already in your worktree:")
            lines += [f"  - {c}" for c in g["commits"]]
    out = ("## Since your last push, on GitHub\n\nSomeone pushed to the PR branch (a conflict resolved by hand, a commit "
           "suggestion). Check the latest code before changing anything, and don't redo what's already there.\n\n"
           + "\n".join(lines) + "\n") if lines else ""
    base = []
    for p in st.get("prs") or []:
        b = p.get("base_merge")
        if not b:
            continue
        if b["merged"]:
            base.append(f"- {p['repo']} #{p.get('number')}: the agent merged the latest `{b['base']}` into the branch "
                        f"({b['behind']} new commits there). Build on top of it.")
        else:
            why = f"it conflicts in {', '.join(b['conflicts'])}" if b["conflicts"] else f"the merge failed: {b.get('error')}"
            base.append(f"- {p['repo']} #{p.get('number')}: the branch is {b['behind']} commits behind `{b['base']}` and {why}. "
                        f"First run `git merge --no-edit origin/{b['base']}` in the worktree, resolve every conflict keeping "
                        "what the PR is for, and commit the merge (never a rebase). Nothing is pushed until it's merged in.")
    if base:
        out += "\n## The latest base branch\n\n" + "\n".join(base) + "\n"
    return out


def write_rework(d, st, req):
    open_prs = [p for p in st["prs"] if (p.get("live") or {}).get("state") in ("open", "draft", None)]
    lines = [f"# Rework for {st['key']}", "", "## What to do", "", req.get("instructions") or
             "Address the new feedback below and anything new in the ticket.", ""]
    if req.get("images"):
        lines += ["## Screenshots I added (look at each one, they show what I saw)", ""] + \
                 [f"- attachments/{n}" for n in req["images"]] + [""]
    if st.get("came_back"):
        lines += [f"The ticket came back in {TR.name} on {st['came_back']} after its PRs were done. Read ticket.md for why.", ""]
    fresh = comments_since(d, st.get("worked_at_ts"))
    if fresh:
        lines += [f"## New in {TR.name} since the last push", ""]
        for c in fresh:
            lines += [f"### {c.get('author')}, {(c.get('created') or '')[:16].replace('T', ' ')}", "", (c.get("body") or "").strip()[:2000], ""]
    if not st.get("rework_mode"):
        rnd = st.get("round") or 2
        earlier = st.get("previous_prs") or st.get("prs") or []
        lines += [f"## Follow-up round {rnd}: a new branch and a new PR", "",
                  "Work as in implement mode on new branches. Earlier PRs, for reference:", ""]
        lines += [f"- round {p.get('round', 1)}: {p['repo']} #{p.get('number')} ({(p.get('live') or {}).get('state') or 'unknown'}): "
                  f"branch `{p.get('branch')}`, {p.get('url', '')}" for p in earlier] or ["- (none recorded)"]
        script = f"{SKILL}/scripts/ticket_worktree.sh"
        fmt = f" --branch-format '{CONV['branch']}'" if CONV["branch"] != "{type}/{key}-{slug}" else ""
        lines += ["", "## Where each new branch starts", ""]
        stacked = set()
        for p in earlier:
            if open_pr(p) and p["repo"] not in stacked:
                stacked.add(p["repo"])
                lines.append(f"- {p['repo']}: the earlier PR is still open, so stack on it: `{script} {WS}/{p['repo']} {st['key']} "
                             f"<type> <new-slug> --base {p['branch']} --round {rnd}{fmt}`, and set the PR's `base` in the manifest to `{p['branch']}`.")
        lines += [f"- Any other repo, or one whose earlier PRs are merged or closed: start from the latest default branch with "
                  f"`--round {rnd}` and no `--base`.",
                  "- Always a new slug and `--round`: the earlier branches exist and are never reused or pushed to.",
                  "- Start the PR body with what the earlier PR missed and point at it, e.g. `Follow-up to #123: ...`."]
        lines += ["", "## Feedback on the earlier PRs (new first)", ""] + feedback_lines(earlier)
        lines += ["", "## Replying to the comments", "", REPLY_HOWTO + " Replies go on the earlier PR, where the comment "
                  "is; write `{new_pr}` where the follow-up PR's link belongs, and the agent fills it in."]
        (d / "rework.md").write_text("\n".join(lines) + "\n")
        return
    if github_changes_text(st):
        lines += [github_changes_text(st)]
    lines += ["## The PRs to change (commit on these branches, in these worktrees)", ""]
    for p in open_prs:
        lines.append(f"- {p['repo']} #{p['number']} {p.get('url', '')}: branch `{p['branch']}`, base `{p['base']}`, "
                     f"worktree `{p['worktree']}`, head on GitHub `{p.get('rework_base', '')[:12]}`"
                     + (" (detached: commit there as usual, the agent pushes to the branch by name)" if p.get("detached") else ""))
    lines += ["", "## Feedback on the PRs (new since the last push first)", ""] + feedback_lines(open_prs)
    if st.get("tracker_updated_at_pr") and same_second(st.get("tracker_updated")) != same_second(st.get("tracker_updated_at_pr")):
        lines += ["", f"The ticket changed in {TR.name} after the PRs opened: read the newest comments in ticket.md."]
    lines += ["", "## Replying to the comments", "", REPLY_HOWTO]
    (d / "rework.md").write_text("\n".join(lines) + "\n")


# ---------- replies to PR comments, in place ----------

def ticket_prs(st):
    """Every PR this ticket has opened, this round's and earlier rounds', by (repo, number)."""
    return {(p["repo"], p.get("number")): p for p in (st.get("previous_prs") or []) + (st.get("prs") or []) if p.get("number")}


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def reply_errors(st, m, lint_text):
    """A reply must answer a real comment by someone else on one of this ticket's PRs, once, and pass the person's
    style. A PR body must not answer or repeat a review comment: that's what the replies are for."""
    errors, prs, seen = [], ticket_prs(st), set()
    for i, r in enumerate(m.get("replies") or [], 1):
        where = f"reply {i}"
        key = (r.get("repo"), r.get("pr"))
        pr = prs.get(key)
        if not pr:
            errors.append(f"{where}: {r.get('repo')} #{r.get('pr')} isn't one of this ticket's PRs")
            continue
        target = next((f for f in (pr.get("live") or {}).get("feedback") or [] if f.get("id") == r.get("to")), None)
        if not target:
            errors.append(f"{where}: no comment {r.get('to')} on {r.get('repo')} #{r.get('pr')} (use an id from rework.md)")
            continue
        if target.get("bot"):
            errors.append(f"{where}: {r.get('to')} is a bot's report, fix it in code and don't reply")
        if key + (r.get("to"),) in seen:
            errors.append(f"{where}: a second reply to {r.get('to')}, keep one")
        seen.add(key + (r.get("to"),))
        body = (r.get("body") or "").strip()
        if not body:
            errors.append(f"{where}: empty reply")
            continue
        e, w = [], []
        lint_text(f"{where} to {target.get('who')}", body, e, w, inline=True)
        errors += e
    # what people said goes in the replies, not in the PR description
    said = [(f, s) for pr in prs.values() for f in (pr.get("live") or {}).get("feedback") or [] if not f.get("bot")
            for s in re.split(r"(?<=[.?!])\s+|\n+", f.get("body") or "") if len(_norm(s)) >= 40]
    for mp in m.get("prs") or []:
        body = _norm(mp.get("body"))
        if not body:
            continue
        quoted = [f for f, sentence in said if _norm(sentence) in body]
        talks_to = [f for pr in prs.values() for f in (pr.get("live") or {}).get("feedback") or []
                    if not f.get("bot") and re.search(rf"@{re.escape(f.get('who') or '~')}\b", mp.get("body") or "", re.I)]
        if quoted or talks_to:
            who = (quoted or talks_to)[0].get("who")
            errors.append(f"{mp['repo']} PR body answers or quotes {who}'s comment: reply to the comment instead, "
                          "and keep the description about the change")
    return errors


def post_replies(st, m, new_prs=None):
    """Post each reply where its comment lives: in the review thread for inline comments, quoting the comment on the
    PR for the others. Runs after the push. Returns (posted, failed) for the history."""
    posted, failed, prs = [], [], ticket_prs(st)
    new_prs = new_prs or {}
    for r in m.get("replies") or []:
        pr = prs.get((r.get("repo"), r.get("pr")))
        target = next((f for f in ((pr or {}).get("live") or {}).get("feedback") or [] if f.get("id") == r.get("to")), None)
        if not pr or not target:
            failed.append({"to": r.get("to"), "error": "comment not found"})
            continue
        body = with_footer((r.get("body") or "").strip().replace("{new_pr}", new_prs.get(r["repo"], "the follow-up PR")))
        repo = f"{ORG}/{r['repo']}"
        try:
            if target["kind"] == "inline":
                out = run([REAL_GH, "api", "-X", "POST", f"repos/{repo}/pulls/{r['pr']}/comments/{target['thread']}/replies",
                           "-f", f"body={body}"])
            else:
                first = next((l for l in (target.get("body") or "").splitlines() if l.strip()), "")[:200]
                out = run([REAL_GH, "api", "-X", "POST", f"repos/{repo}/issues/{r['pr']}/comments",
                           "-f", f"body=> {first}\n\n{body}"])
            url = (json.loads(out.stdout or "{}") or {}).get("html_url")
            pr.setdefault("replied", {})[r["to"]] = url or True
            posted.append({"to": r["to"], "who": target.get("who"), "pr": r["pr"], "url": url})
        except Exception as exc:
            failed.append({"to": r["to"], "who": target.get("who"), "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    if failed:
        log(f"{st['key']} couldn't post {len(failed)} replies: {failed[0].get('error')}")
        notify(f"{st['key']}: {len(failed)} replies not posted", "The ticket desk lists them under the rework")
    return posted, failed


def finalize_rework(key):
    """Check only the rework's new commits, then push them to the same PRs. Never force, never another branch."""
    st = load(key)
    m = json.loads(Path(st["manifest"]).read_text())
    mine = {p["repo"]: p for p in st["prs"]}
    a = CFG["author"]
    names = TR.mentions(st)
    errors, todo = [], []
    sys.path.insert(0, str(REVIEW_SKILL / "scripts"))
    from post_review import lint_text  # noqa: E402
    for mp in m.get("prs") or []:
        cur = mine.get(mp["repo"])
        if not cur or not cur.get("rework_base"):
            errors.append(f"{mp['repo']}: not one of this ticket's open PRs")
            continue
        wt, branch, base_sha = cur["worktree"], cur["branch"], cur["rework_base"]
        if git("-C", wt, "branch", "--show-current") != ("" if cur.get("detached") else branch):
            errors.append(f"{mp['repo']}: worktree isn't on {branch}")
            continue
        if Path(wt).parent != WS / ".local" / "wt":
            errors.append(f"{mp['repo']}: the worktree {wt} isn't the agent's own (under {WS / '.local' / 'wt'}), rework again")
            continue
        pr_base = cur.get("base") or default_branch(repo_dir(mp["repo"]), mp["repo"])
        git("-C", wt, "fetch", "--quiet", "origin", f"+refs/heads/{pr_base}:refs/remotes/origin/{pr_base}")
        if git("-C", wt, "status", "--porcelain", "--untracked-files=no"):
            errors.append(f"{mp['repo']}: uncommitted changes in the worktree")
        untracked = [l[3:] for l in git("-C", wt, "status", "--porcelain").splitlines() if l.startswith("??") and not NOISE.search(l[3:])]
        if untracked:
            errors.append(f"{mp['repo']}: files left out of the commit: {', '.join(untracked[:5])}")
        if run([REAL_GIT, "-C", wt, "merge-base", "--is-ancestor", base_sha, "HEAD"], check=False).returncode != 0:
            errors.append(f"{mp['repo']}: the rework rewrote commits that are already on the PR, nothing is force-pushed")
            continue
        if st.get("fix_conflict") and not is_ancestor(wt, f"refs/remotes/origin/{pr_base}", "HEAD"):
            errors.append(f"{mp['repo']}: the fix doesn't merge {pr_base} into {branch}, so the conflict is still there")
            continue
        if cur.get("base_tip") and not is_ancestor(wt, cur["base_tip"], "HEAD"):
            errors.append(f"{mp['repo']}: the latest {pr_base} isn't merged into {branch}. Merge origin/{pr_base}, "
                          "resolve the conflicts and commit the merge, then run the PR step again")
            continue
        # only the rework's own commits: what a merge of the base branch brought in is teammates' work, already reviewed
        commits = [c for c in git("-C", wt, "log", "--format=%H%x00%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e", f"{base_sha}..HEAD",
                                  f"^refs/remotes/origin/{pr_base}").split("\x1e") if c.strip()]
        for c in commits:
            sha, parents, an, ae, cn, ce, msg = c.strip("\n").split("\x00", 6)
            if (an, ae.lower()) != (a["name"], a["email"].lower()) or (cn, ce.lower()) != (a["name"], a["email"].lower()):
                errors.append(f"{mp['repo']}: commit {sha[:7]} isn't authored and committed by {a['name']} <{a['email']}>")
            # a merge that brings the base branch in keeps git's own message
            merges_base = len(parents.split()) > 1 and any(is_ancestor(wt, par, f"refs/remotes/origin/{pr_base}") for par in parents.split()[1:])
            if not merges_base and not any(re.search(re.escape(n) + r"(?!\d)", msg) for n in names):
                errors.append(f"{mp['repo']}: commit {sha[:7]} message doesn't mention {key}")
            bad = attribution_error(mp["repo"], sha, msg, merge=len(parents.split()) > 1)
            if bad:
                errors.append(bad)
        for f in git("-C", wt, "diff", "--name-only", f"refs/remotes/origin/{pr_base}...HEAD").splitlines():
            if SENSITIVE.search(f):
                errors.append(f"{mp['repo']}: refuses to push a secrets-looking file: {f}")
        title = mp.get("title") or (cur.get("live") or {}).get("title") or cur.get("title")
        if title != cur.get("title") and not re.fullmatch(title_re(key), title or ""):
            errors.append(f"{mp['repo']}: title must follow '{CONV['pr_title']}', got '{title}'")
        body = (mp.get("body") or "").strip()
        if body and body != (cur.get("body") or "").strip():
            e, w = [], []
            template = pr_template(mp["repo"])
            lint_text(f"{mp['repo']} PR body", body, e, w, inline=False, structure=not template)
            errors += e + (template_errors(mp["repo"], body, template[1]) if template else [])
        remote = run([REAL_GIT, "-C", wt, "ls-remote", "origin", f"refs/heads/{branch}"], check=False).stdout.split("\t")[0].strip()
        if remote and remote != base_sha and run([REAL_GIT, "-C", wt, "merge-base", "--is-ancestor", remote, "HEAD"], check=False).returncode != 0:
            errors.append(f"{mp['repo']}: someone pushed to {branch} during the rework, so it wasn't pushed. Rework again to build on it")
        todo.append((cur, mp, commits, title, body))
    errors += reply_errors(st, m, lint_text)
    if errors:
        return record_failure(st, "pr", "\n".join(errors), phase="needs_attention", errors=errors)
    summary = {}
    try:
        for cur, mp, commits, title, body in todo:
            wt = cur["worktree"]
            if commits:
                git("-C", wt, "push", "--quiet", "origin", f"HEAD:refs/heads/{cur['branch']}")
                cur["pushed_sha"] = git("-C", wt, "rev-parse", "HEAD")
            edit = []
            if title and title != cur.get("title"):
                edit += ["--title", title]
                cur["title"] = title
            if body and body != (cur.get("body") or "").strip():
                f = RUNS / key / st["run_id"] / f"{cur['repo']}-body.md"
                line = TR.issue_line(st)
                f.write_text(with_footer(body + (f"\n\n{line}" if line and not links_ticket(st, body) else "")) + "\n")
                edit += ["--body-file", str(f)]
                cur["body"] = body
            if edit:
                run([REAL_GH, "pr", "edit", str(cur["number"]), "--repo", f"{ORG}/{cur['repo']}", *edit])
            cur["feedback_since"] = utc_iso()
            cur.pop("rework_base", None)
            cur.pop("github_changes", None)
            cur.pop("base_merge", None)
            cur.pop("base_tip", None)
            summary[cur["repo"]] = {"commits": len(commits), "changes": mp.get("changes") or ""}
    except Exception as exc:
        return record_failure(st, "pr", f"{type(exc).__name__}: {exc}"[:400], phase="needs_attention", errors=[str(exc)[:400]])
    replied, unreplied = post_replies(st, m)
    st.setdefault("reworks", []).append({"at": now(), "instructions": (st.get("rework_last") or {}).get("instructions"),
                                         "summary": m.get("summary"), "prs": summary, "replies": replied,
                                         "replies_failed": unreplied})
    st.update({"rework_mode": False, "came_back": None, "new_feedback": 0, "failure": None, "diagnosis": None,
               "worked_at_ts": time.time(), "tracker_updated_at_pr": st.get("tracker_updated"),
               "fix_conflict": False, "conflict_request": None})
    pushed = [f"{r} +{v['commits']}" for r, v in summary.items() if v["commits"]]
    notify(f"Rework pushed for {key}", ", ".join(pushed) or "no code changes were needed")
    log(f"{key} rework pushed: {', '.join(pushed) or 'no changes'}")
    return set_phase(st, "pr_open", error=None)


# ---------- tell the agent: plain words in, a plan made of the desk's own actions out ----------

TELL_ACTIONS = {
    "note": "add what the person said to the ticket's context, so every later run reads it",
    "park": "skip the ticket for now (it comes back with unpark)",
    "unpark": "bring a parked ticket back where it was",
    "close": "mark it closed: only when the tracker shows a done status or every PR is merged or closed",
    "refresh_prs": "read the ticket's PRs from GitHub again (merged, closed, ready, conflicts)",
    "check": "check the ticket again from the start",
    "fix_conflict": "merge the base into the agent's own PR branch, resolve, test and push (needs GitHub to show a conflict)",
    "rework": "more work on the ticket's PRs, or a follow-up PR, with instructions",
    "link_pr": "make an open PR of the developer's own (one they opened by hand) this ticket's PR, so reworks add commits to it",
    "context": "save the developer's answer to the agent's questions with the ticket, so every later run reads it",
    "approve": "approve a checked ticket and implement it (ready, needs_context or already_done)",
    "fix": "run the failed step again: resume its session with instructions, start over, or only the PR step",
    "answer": "nothing to change, the reply is the answer",
}
DONE_STATUSES = ("done", "closed", "resolved", "ready to deploy", "deployed", "released", "won't do", "won't fix", "cancelled", "canceled")

TELL_PROMPT = """You are the ticket agent, talking with the developer about ticket {key} on their ticket desk. They wrote:

{message}
{shots}
Find out what they mean and what the ticket needs, then propose a plan. This is read-only: don't edit files, don't
commit, don't push, don't change branches, don't write to the tracker or GitHub. Read and run read-only commands.

What you can read:
- the ticket folder (ticket.md with the newest comments and status, attachments, context.md, earlier runs): {ticket_dir}
- the ticket's state on the desk (JSON): {state}
- its PRs on GitHub: `gh pr view <n> --repo {org}/<repo> --json state,isDraft,mergeable,mergeStateStatus,reviewDecision,headRefOid,commits`
  and `gh pr diff`, `gh api repos/{org}/<repo>/pulls/<n>/comments`
- the agent's worktrees for this ticket: {worktrees}

The plan can only use these actions, in order, each one an object with "do" and its fields:
{actions}
Fields: "note", "rework" and "context" take "text" (in the developer's words plus anything you found);
"fix_conflict" takes "instructions"; "fix" takes "instructions" and "how" (resume, fresh or pr_step);
"link_pr" takes "repo" and "number". The others take nothing.

Rules:
- Check claims before acting on them. "I closed it" means close only when ticket.md shows a done status or every PR is
  merged or closed; otherwise answer with what you see. "I fixed the conflict" means refresh_prs, and say whether GitHub
  still shows a conflict. "I pushed a fix by hand" means refresh_prs; a later rework starts from what is on GitHub.
- Prefer the smallest plan. Never propose rework or fix_conflict just to be safe.
- The developer wants the work on a PR they opened themselves that isn't one of the ticket's PRs: link_pr it, then rework.
  Only their own open PRs can be linked, and a rework then adds commits to that PR's branch (never a stacked PR).
- The ticket failed and its diagnosis says it needs the developer (fix.action "needs_you"): never propose fix or rework
  that would run into the same wall again. Propose what removes the cause (often link_pr), or answer and ask.
- An answer to the agent's questions goes in context, then approve or check as the developer wants.
- If the message is unclear, propose no actions and put one short question in "questions".

Write this JSON to {out}:
{{"understood": "one sentence: what they asked, in your words",
  "reply": "2-4 plain sentences to the developer: what you found (with the evidence, e.g. PR state or a status) and what the plan does",
  "actions": [{{"do": "..."}}],
  "questions": []}}
End with the line RESULT: written."""


def request_tell(key, message, images=()):
    """Queue a read-only run that reads the ticket and proposes a plan for what the person wrote."""
    st = known(key)
    message = (message or "").strip()
    if not message:
        raise SystemExit("write what you want first")
    if (st.get("tell") or {}).get("state") == "thinking" and OS.lock_held(lock_file(f"{key}.tell")):
        raise SystemExit("the agent is still reading the last message")
    shots = [Path(n).name for n in images if (TICKETS / key / "attachments" / Path(n).name).is_file()]
    if st.get("tell"):
        # the earlier exchange stays readable on the desk
        st["tell_history"] = ((st.get("tell_history") or []) + [st["tell"]])[-TELL_KEEP:]
    st["tell"] = {"state": "thinking", "id": time.strftime("%Y%m%d-%H%M%S"), "message": message, "images": shots, "at": now()}
    touch_user(st)
    save(st)
    log(f"{key} tell: {message[:120]}")
    spawn("_tell", key)
    return "Reading the ticket, its PRs and the tracker. The plan shows up here in a minute or two."


TELL_KEEP = 100


def tell_thread(st, log_lines):
    """Every exchange on this ticket's Tell box, oldest first. The kept ones carry the full message; older ones, from
    before the desk kept them, are read back from the log (their message is cut at 120 characters there) and their
    saved plan."""
    key = st["key"]
    kept = (st.get("tell_history") or []) + ([st["tell"]] if st.get("tell") else [])
    plans = {}
    for f in (TICKETS / key).glob("tell-*.json"):
        tid = f.stem[5:]
        try:
            plans[f"{tid[:4]}-{tid[4:6]}-{tid[6:8]} {tid[9:11]}:{tid[11:13]}"] = (tid, json.loads(f.read_text()))
        except (ValueError, OSError):
            continue
    old, open_msg = [], False
    for line in log_lines:
        if not re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d ", line):
            # a message with line breaks goes on over the next lines of the log
            if open_msg:
                old[-1]["message"] += "\n" + line
            continue
        at, said = line[:16], line[17:]
        open_msg = said.startswith(f"{key} tell: ")
        if open_msg:
            old.append({"at": at, "message": said[len(key) + 7:], "state": "proposed"})
        elif said.startswith(f"{key} tell done: ") and old:
            old[-1].update(state="done", done=[x.strip() for x in said[len(key) + 12:].split(";") if x.strip()], done_at=at)
    for t in old:
        # the log keeps the first 120 characters
        t["partial"] = len(t["message"]) >= 120
    seen = {t.get("at") for t in kept}
    for t in old:
        tid, plan = plans.get(t["at"], (None, {}))
        t.update(id=tid, **{k: plan.get(k) for k in ("reply", "understood", "actions", "questions") if plan.get(k)})
    return sorted([t for t in old if t["at"] not in seen] + kept, key=lambda t: t.get("at") or "")


def clean_plan(plan, st):
    """Keep only known actions with their known fields. Returns the plan, and what was dropped."""
    out, dropped = [], []
    for a in plan.get("actions") or []:
        do = (a or {}).get("do")
        if do not in TELL_ACTIONS:
            dropped.append(str(do))
            continue
        keep = {"do": do}
        if do in ("note", "rework", "context") and (a.get("text") or "").strip():
            keep["text"] = a["text"].strip()[:4000]
        if do in ("fix_conflict", "fix"):
            keep["instructions"] = (a.get("instructions") or "").strip()[:4000]
        if do == "fix":
            keep["how"] = a.get("how") if a.get("how") in ("resume", "fresh", "pr_step") else "resume"
        if do == "link_pr":
            try:
                keep.update(repo=str(a.get("repo") or "").split("/")[-1], number=int(a.get("number")))
            except (TypeError, ValueError):
                dropped.append("link_pr without a PR number")
                continue
        out.append(keep)
    return {"understood": str(plan.get("understood") or "")[:400], "reply": str(plan.get("reply") or "")[:2000],
            "questions": [str(q)[:300] for q in plan.get("questions") or []][:3], "actions": out[:6], "dropped": dropped}


def do_tell(key):
    lock = open(lock_file(f"{key}.tell"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    slot = take_slot("tell")
    while not slot:
        time.sleep(15)
        slot = take_slot("tell")
    st = load(key) or {}
    t = st.get("tell") or {}
    if t.get("state") != "thinking":
        return
    tid = t["id"]

    def update(**fields):
        # a newer message may have arrived meanwhile: only touch this one
        cur = load(key) or {}
        if (cur.get("tell") or {}).get("id") != tid:
            return
        cur["tell"] = {**cur["tell"], **fields}
        save(cur)
    try:
        try:
            sync_ticket_prs(key)
        except Exception:
            pass
        st = load(key) or st
        d = prepare_ticket(key, st)
        save(st)
        out = d / f"tell-{tid}.json"
        out.unlink(missing_ok=True)
        wts = sorted({str(p["worktree"]) for p in st.get("prs") or [] if p.get("worktree")})
        view = {k: st.get(k) for k in ("phase", "status", "round", "prs", "previous_prs", "failure", "diagnosis", "rework_mode",
                                       "came_back", "assessment")}
        shots = "".join(f"\nScreenshot they added: {d / 'attachments' / n}" for n in t.get("images") or [])
        prompt = TELL_PROMPT.format(key=key, message=t["message"], shots=shots + "\n" if shots else "", ticket_dir=d,
                                    state=json.dumps(view, default=str)[:12000], org=ORG, worktrees=", ".join(wts) or "none",
                                    actions="\n".join(f"- {k}: {v}" for k, v in TELL_ACTIONS.items()), out=out)
        res, text, facts = claude_run(prompt, CFG.get("tell_budget_usd", 2), CFG.get("tell_timeout_minutes", 10),
                                      LOGS / f"{key}-tell-{tid}.json", [d] + [Path(w) for w in wts], "tell", st, read_only=True)
        cost = res.get("total_cost_usd") or 0
        cur = load(key) or {}
        cur["cost_tell"] = (cur.get("cost_tell") or 0) + cost
        save(cur)
        if not out.exists():
            update(state="failed", error=(text or "it wrote no plan")[-400:], cost=cost)
            return
        plan = clean_plan(json.loads(out.read_text()), st)
        update(state="proposed", cost=cost, at_plan=now(), **plan)
        log(f"{key} tell plan: {', '.join(a['do'] for a in plan['actions']) or 'answer only'}")
    except Exception as exc:
        update(state="failed", error=f"{type(exc).__name__}: {exc}"[:400])


def closed_evidence(st):
    """Why the ticket counts as closed, or None. The tracker's status, or every PR merged or closed on GitHub."""
    status = (st.get("status") or "").strip().lower()
    if status in DONE_STATUSES:
        return f"{TR.name} says {st.get('status')}"
    prs = [p for p in st.get("prs") or [] if p.get("number")]
    if prs and all((p.get("live") or {}).get("state") in ("merged", "closed") for p in prs):
        return "every PR is merged or closed on GitHub"
    return None


def apply_tell(key, tell_id):
    """Do it: run the plan's actions, in order, through the desk's own code. Stops at the first that's refused."""
    st = known(key)
    t = st.get("tell") or {}
    if t.get("state") != "proposed" or t.get("id") != tell_id:
        raise SystemExit("this plan isn't the current one any more, send the message again")
    done = []
    for a in t.get("actions") or []:
        do = a["do"]
        st = known(key)
        if do == "note":
            st.setdefault("context", []).append({"at": now(), "text": a.get("text") or t["message"]})
            touch_user(st)
            save(st)
            done.append("Noted")
        elif do == "park":
            if st["phase"] != "skipped":
                st["phase_before_skip"] = st["phase"]
                set_phase(st, "skipped")
            done.append("Parked")
        elif do == "unpark":
            if st["phase"] == "skipped":
                set_phase(st, st.get("phase_before_skip") or "new")
            done.append("Back on the list")
        elif do == "close":
            why = closed_evidence(st)
            if not why:
                raise SystemExit(f"{TR.name} still shows {st.get('status') or 'it open'} and its PRs aren't all merged or closed, "
                                 "so it isn't marked closed. Close it there first")
            st["closed_because"] = why
            set_phase(st, "closed", closed_at=now())
            done.append(f"Closed ({why})")
        elif do == "refresh_prs":
            sync_ticket_prs(key)
            done.append("PRs read from GitHub")
        elif do == "check":
            set_phase(st, "check_queued", error=None)
            start_queued()
            done.append("Checking again")
        elif do == "fix_conflict":
            done.append(request_conflict_fix(key, a.get("instructions") or t["message"]))
        elif do == "rework":
            done.append(request_rework(key, a.get("text") or t["message"], t.get("images") or []))
        elif do == "link_pr":
            done.append(link_pr(key, a["repo"], a["number"]))
        elif do == "context":
            st.setdefault("context", []).append({"at": now(), "text": a.get("text") or t["message"]})
            touch_user(st)
            save(st)
            done.append("Answer saved")
        elif do == "approve":
            if st["phase"] not in ("ready", "needs_context", "already_done"):
                raise SystemExit(f"{key} is {st['phase'].replace('_', ' ')}, only a checked ticket can be approved")
            set_phase(st, "approved", error=None, approved_at=now())
            start_queued()
            done.append("Approved, implementing")
        elif do == "fix":
            how = {"resume": "resume", "fresh": "rerun", "pr_step": "rerun_pr_step"}[a.get("how") or "resume"]
            done.append(request_fix(key, a.get("instructions") or t["message"], how).rstrip("."))
        elif do == "answer":
            done.append("Nothing to change")
    st = known(key)
    st["tell"] = {**(st.get("tell") or {}), "state": "done", "done": done, "done_at": now()}
    save(st)
    log(f"{key} tell done: {'; '.join(done)}")
    return ". ".join(done) + "."


def dismiss_tell(key):
    st = known(key)
    if st.get("tell"):
        st["tell"] = {**st["tell"], "state": "dismissed"}
        save(st)
    return "Dismissed, nothing changed."


# ---------- the poll ----------

def old_failure(st):
    """A failure record for a failure kept before records existed, explained from its error. None if not failed."""
    if st.get("phase") not in ("failed", "needs_attention") or st.get("failure") or not st.get("error"):
        return None
    step = st.get("failed_step") or "implement"
    g = {"id": f"{step}-old", "step": step, "message": st["error"], "at": st.get("state_updated"),
         "errors": st["error"].split("\n") if step == "pr" else None,
         "session_id": st.get(f"{'check' if step == 'check' else 'implement'}_session")}
    g["explained"] = explain(g)
    return g


def heal_stopped_runs(paused=False):
    for st in all_states():
        f = st.get("failure") or {}
        g = old_failure(st)
        if g:
            st["failure"] = g
            save(st)
        # a run stopped by the usage limit resumes once, after the limit resets
        if not paused and st.get("phase") == "failed" and f.get("retry_at") and not f.get("retried") and time.time() >= f["retry_at"]:
            f["retried"] = True
            st["failure"] = f
            save(st)
            log(f"{st['key']} usage limit reset, resuming")
            request_fix(st["key"], "The Claude usage limit has reset. Continue exactly where you stopped.", "resume")
            continue
        if st.get("phase") in WORKING and not is_running(st["key"]):
            step = "check" if st["phase"] == "checking" else "implement"
            prog = progress.view(PROGRESS / f"{st['key']}.json", 12) or {}
            record_failure(st, step, st.get("error") or "the run stopped before it finished",
                           {"session_id": prog.get("session_id"), "last_steps": [x.get("text") for x in prog.get("steps") or []][-8:],
                            "duration_s": prog.get("elapsed_s")}, stopped=True, quiet=paused)


def spawn(*args):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "runs.log", "a") as out:
        subprocess.Popen([*OS.LOW, sys.executable, str(BASE / "agent.py"), *args], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def came_back_move(old, new):
    """True when a ticket went from a status that means "out of my hands" to one that means "work on it": a QA
    bounce from Testing to In Progress. Needs ticket.active_statuses (the statuses that mean work is expected),
    which is empty by default, so nothing is flagged until the person says which statuses those are."""
    active = {x.strip().lower() for x in CFG.get("active_statuses") or []}
    return bool(active) and bool(old) and bool(new) and old != new and new.strip().lower() in active \
        and old.strip().lower() not in active


def sync_tracker():
    """Read the assigned tickets. Returns False when the tracker couldn't be read, the rest of the cycle goes on."""
    try:
        issues = TR.search()
        me = TR.whoami()
        tracker_status(bool(me.get("ok")), name=me.get("name"), error=me.get("error"))
    except SignInNeeded as exc:
        prev = json.loads(TRACKER_STATUS.read_text()) if TRACKER_STATUS.exists() else {}
        if prev.get("ok", True):
            notify(f"{TR.name} sign-in needed", "Open the ticket desk to see what to do")
        tracker_status(False, error=str(exc))
        return False
    except Exception as exc:
        # a slow or failed GitHub call must not stop the PR sync and the runs
        tracker_status(False, error=f"couldn't read {TR.name}: {type(exc).__name__}: {str(exc)[:200]}")
        log(f"tracker read failed: {type(exc).__name__}: {str(exc)[:200]}")
        return False
    seen = set()
    for i in issues:
        key = i["key"]
        seen.add(key)
        st = load(key) or {"key": key, "phase": "new", "context": [], "first_seen": now()}
        if st.get("status") and i["status"] != st.get("status"):
            log(f"{key} status {st.get('status')} -> {i['status']} in {TR.name}")
        if st.get("phase") == "closed" and i["status"].strip().lower() not in DONE_STATUSES and came_back_move(st.get("status"), i["status"]):
            # closed on the desk, then reopened in the tracker: the desk follows the tracker
            log(f"{key} reopened in {TR.name}: {st.get('status')} -> {i['status']}")
            notify(f"{key} reopened", f"{TR.name} moved it to {i['status']}")
            st["phase"] = "pr_open" if any(p.get("number") for p in st.get("prs") or []) else "new"
            st["came_back"] = now()
        after_pr = st.get("phase") in ("pr_open", "merged", "pr_closed")
        if after_pr and not st.get("came_back"):
            if st.get("gone"):
                # done or closed before, and it's back on the list: the ticket came back
                reason = "is back on your list"
            elif came_back_move(st.get("status"), i["status"]):
                # still on the list, but moved from Testing (or similar) back to a working status since the last poll
                reason = f"moved from {st.get('status')} to {i['status']}"
            else:
                reason = None
            if reason:
                st["came_back"] = now()
                log(f"{key} came back in {TR.name}: {reason}")
                notify(f"{key} came back", "Open it on the ticket desk to rework it")
        st.update({"summary": i["summary"], "type": i["type"], "status": i["status"], "priority": i["priority"],
                   "sprint": i.get("sprint"), "tracker_updated": i["updated"], "url": i["url"], "source": i["source"], "gone": False})
        if st["phase"] == "new" and st["type"] in CFG["skip_types"]:
            st["phase"] = "not_implementable"
            st["assessment"] = {"verdict": "not_implementable", "summary": f"{st['type']}s aren't implemented directly."}
        save(st)
    for st in all_states():
        if st["key"] not in seen and not st.get("gone"):
            st["gone"] = True
            save(st)
    return True


def cmd_cycle(_):
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    paused = PAUSE.exists()
    LAST_CYCLE.write_text(now())
    # after a restart nothing else starts the desk
    if not DESK_STOPPED.exists():
        start_desk()
    # reading the tracker and the PRs is free and never writes anything, so it goes on while paused
    heal_stopped_runs(paused)
    sync_tracker()
    sync_prs()
    if paused:
        return
    states = [s for s in all_states() if not s.get("gone")]
    if CFG["auto_check"] == "all":
        due = [s for s in states if s["phase"] == "new" and not is_running(s["key"])]
        # tickets edited since the last check get checked again, at most once per cooldown
        due += [s for s in states if s["phase"] in ("needs_context", "ready", "already_done")
                and s.get("checked_tracker_updated")
                and same_second(s.get("tracker_updated")) != same_second(s.get("checked_tracker_updated"))
                and time.time() - (s.get("checked_at_ts") or 0) > AUTO_RECHECK_COOLDOWN_S]
        for s in due[:CFG["checks_per_cycle"]]:
            set_phase(s, "check_queued", error=None)
    start_queued()


# ---------- commands ----------

def then_next(job_fn, key):
    """Run one job, then hand its freed slot to the next queued ticket."""
    job_fn(check_key(key))
    start_queued()


def known(key):
    st = load(key)
    if not st:
        sys.exit(f"{key} isn't known yet, the next cycle picks up tickets assigned to you")
    return st


def priority_rank(p):
    """P0 first, then the usual words, then anything else, then no priority."""
    p = (p or "").strip().lower()
    m = re.fullmatch(r"p(\d+)", p)
    if m:
        return (0, int(m.group(1)), "")
    words = ["blocker", "highest", "critical", "urgent", "high", "medium", "normal", "low", "lowest", "trivial"]
    return (1, words.index(p), "") if p in words else ((2, 0, p) if p else (3, 0, ""))


def sprint_rank(sp, today=None):
    """Current sprint first, then upcoming by start, then past ones newest first, then no sprint."""
    if not sp or not sp.get("start"):
        return (3, 0)
    day = lambda d: int(time.mktime(time.strptime(d, "%Y-%m-%d")) // 86400)
    start, now_day = day(sp["start"]), day(today or time.strftime("%Y-%m-%d"))
    if start <= now_day < start + int(sp.get("days") or 0):
        return (0, start)
    return (1, start) if start > now_day else (2, -start)


def cmd_list(_):
    rows = sorted((s for s in all_states() if not s.get("gone") or s["phase"] == "pr_open"),
                  key=lambda s: (sprint_rank(s.get("sprint")), priority_rank(s.get("priority")), s["key"]))
    group = object()
    for s in rows:
        sp = (s.get("sprint") or {}).get("title") or "No sprint"
        if sp != group:
            print(f"\n{sp}" + (" (current)" if sprint_rank(s.get("sprint"))[0] == 0 else ""))
            group = sp
        print(f"  {s['key']:<12} {s.get('priority') or '-':<4} {s['phase']:<18} {s.get('status', ''):<14} {(s.get('summary') or '')[:56]}")
    if not rows:
        print("no tickets yet, run: ptk cycle")


def cmd_show(arg):
    key = check_key(arg)
    st = known(key)
    print(f"{key} [{st.get('type')}, {st.get('status')}] {st.get('summary')}\n{st.get('url') or ''}\nphase: {st['phase']}")
    a = st.get("assessment") or {}
    for label, k in (("summary", "summary"), ("verification", "verification"), ("risk", "risk")):
        if a.get(k):
            print(f"{label}: {a[k]}")
    for k in ("plan", "questions"):
        if a.get(k):
            print(f"{k}:\n" + "\n".join(f"  - {x}" for x in a[k]))
    for p in st.get("prs") or []:
        print(f"PR {p['repo']}: {p.get('url') or p.get('branch')}")
    if st.get("error"):
        print(f"error:\n{st['error']}")


def cmd_check(arg):
    key = check_key(arg)
    st = known(key)
    if FLAGS:
        set_job_override(st, "check", FLAGS.get("model"), FLAGS.get("effort"))
        touch_user(st)
        save(st)
    print(f"checking {key} on {RS.describe(*job_choice(st, 'check'), RS.load(RUN_SETTINGS, JOBS))}, this takes a few minutes")
    st = do_check(key)
    print(f"{key}: {st['phase']}")


def cmd_context(arg):
    key = check_key(arg)
    text = sys.argv[3] if len(sys.argv) > 3 else ""
    if not text.strip():
        sys.exit('usage: ptk context <KEY> "<text>" [--implement]')
    st = known(key)
    st.setdefault("context", []).append({"at": now(), "text": text.strip()})
    touch_user(st)
    implement = "--implement" in sys.argv[4:]
    if FLAGS:
        set_job_override(st, "implement" if implement else "check", FLAGS.get("model"), FLAGS.get("effort"))
    set_phase(st, "approved" if implement else "new", error=None)
    if implement:
        print(f"context saved, {key} is queued for implementation")
    else:
        set_phase(st, "check_queued")
        print(f"context saved, checking {key} again")
    start_queued()


def cmd_approve(arg):
    key = check_key(arg)
    st = known(key)
    notes = sys.argv[3] if len(sys.argv) > 3 else ""
    if notes.strip():
        st.setdefault("context", []).append({"at": now(), "text": f"Notes when approving: {notes.strip()}"})
        touch_user(st)
    if FLAGS:
        set_job_override(st, "implement", FLAGS.get("model"), FLAGS.get("effort"))
        touch_user(st)
    set_phase(st, "approved", error=None, approved_at=now())
    print(f"{key} approved")
    if parallel_limit("implement") - running_count("implement") <= 0:
        print(f"{running_count('implement')} tickets are being implemented, this one starts when one finishes")
    start_queued()


def cmd_finalize(arg):
    key = check_key(arg)
    st = finalize(key, dry_run="--dry-run" in sys.argv[3:])
    if st.get("dry_run_ok"):
        return
    print(f"{key}: {st['phase']}\n{st.get('error') or ''}".strip())


def cmd_repush(arg):
    """The PR step again for a rework: read the PR branch from GitHub first, then check and push."""
    key = check_key(arg)
    st = known(key)
    errs = prepare_rework(st)
    if errs:
        record_failure(st, "pr", "\n".join(errs), phase="needs_attention", errors=errs, rework=True)
        return
    save(st)
    finalize_rework(key)


def cmd_rework(arg):
    """ptk rework <KEY> ["what to change"] [--image FILE ...]"""
    key = check_key(arg)
    args, files, rest = sys.argv[3:], [], []
    i = 0
    while i < len(args):
        if args[i] == "--image":
            if i + 1 >= len(args):
                sys.exit("--image needs a file")
            files.append(Path(args[i + 1]).expanduser())
            i += 2
        else:
            rest.append(args[i])
            i += 1
    rest = [a for a in rest if not a.startswith("--")]
    for f in files:
        if not f.is_file():
            sys.exit(f"no such file: {f}")
    known(key)
    images = [save_attachment(key, f.name, f.read_bytes()) for f in files]
    print(request_rework(key, rest[0] if rest else "", images))


def cmd_prs(arg):
    """ptk prs [<KEY>]"""
    keys = [check_key(arg)] if arg else [s["key"] for s in all_states()]
    for key in keys:
        st = known(key)
        if not any(p.get("number") for p in st.get("prs") or []):
            if arg:
                print(f"{key} has no PRs yet")
            continue
        if not sync_ticket_prs(key):
            print(f"{key}: left as it is ({'running now' if is_running(key) else st.get('phase')})")
        for p in (load(key) or {}).get("prs") or []:
            live = p.get("live") or {}
            print(f"{key}  {p['repo']} #{p.get('number')}  {live.get('state') or 'unknown'}"
                  + (f", {live['review']}" if live.get("review") else "") + (f"  ({p['live_error']})" if p.get("live_error") else ""))


def cmd_fix(arg):
    """ptk fix <KEY> ["instructions"] [--fresh | --pr-step]"""
    key = check_key(arg)
    rest = [a for a in sys.argv[3:] if not a.startswith("--")]
    action = "rerun" if "--fresh" in sys.argv else "rerun_pr_step" if "--pr-step" in sys.argv else "resume"
    print(request_fix(key, rest[0] if rest else "", action))


def cmd_skip(arg):
    key = check_key(arg)
    st = known(key)
    st["phase_before_skip"] = st["phase"]
    set_phase(st, "skipped")
    print(f"{key} skipped")


def cmd_unskip(arg):
    key = check_key(arg)
    st = known(key)
    set_phase(st, st.get("phase_before_skip") or "new")
    print(f"{key} back to {st['phase']}")


def cmd_login(_):
    if not TR.can_login:
        sys.exit(f"{TR.name} needs no browser sign-in here. "
                 + ("Run: gh auth login --hostname " + TR.host if TR.name == "GitHub" else "Check the token in the config."))
    out = subprocess.run([str(VENV_PY), str(BASE / "trackers" / "jira.py"), "login"], capture_output=True, text=True)
    res = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {"ok": False, "error": out.stderr[-200:]}
    me = TR.whoami() if res.get("ok") else res
    tracker_status(bool(me.get("ok")), name=me.get("name"), error=me.get("error"))
    print(f"signed in to {TR.name}" if me.get("ok") else f"sign-in failed: {me.get('error')}")


def take_flags():
    """Pull --model and --effort out of argv, so the other arguments keep their places."""
    out = {}
    for f in ("--model", "--effort"):
        if f in sys.argv:
            i = sys.argv.index(f)
            if i + 1 < len(sys.argv):
                out[f[2:]] = sys.argv[i + 1]
                del sys.argv[i:i + 2]
    return out


def cmd_settings(arg):
    """ptk settings [check|implement|diagnose] [--model M] [--effort E]"""
    data = RS.load(RUN_SETTINGS, JOBS)
    if arg and arg not in JOBS:
        sys.exit("usage: ptk settings [check|implement|diagnose] [--model M] [--effort E]")
    if FLAGS:
        if not arg:
            sys.exit("say which job: ptk settings check ... or ptk settings implement ...")
        m, e = RS.clean(FLAGS.get("model"), FLAGS.get("effort"), data)
        if FLAGS.get("model") not in (None, "default") and not m:
            sys.exit(f"model must be one of: {', '.join(x['id'] for x in data['models'])} (ptk models re-checks)")
        if FLAGS.get("effort") not in (None, "default") and not e:
            sys.exit(f"effort must be one of: {', '.join(RS.EFFORTS)}")
        d = data["defaults"][arg]
        if FLAGS.get("model") is not None:
            d["model"] = None if FLAGS["model"] == "default" else m
        if FLAGS.get("effort") is not None:
            d["effort"] = None if FLAGS["effort"] == "default" else e
        RS.save(RUN_SETTINGS, data)
    for j in JOBS:
        d = data["defaults"][j]
        print(f"{dict(check='checks', implement='implementations', diagnose='diagnoses')[j]} run on {RS.describe(d.get('model'), d.get('effort'), data)}"
              f", {parallel_limit(j)} at once")
    print("models that work: " + (", ".join(f"{x['id']} ({x['label']})" for x in data["models"]) or "not checked yet, run ptk models"))
    for st in all_states():
        for j, v in (st.get("run_settings") or {}).items():
            print(f"  {st['key']} {j}: {RS.describe(v.get('model'), v.get('effort'), data)}")


def cmd_models(_):
    data = RS.load(RUN_SETTINGS, JOBS)
    print("checking which models your Claude setup allows, a few cents each")
    data["models"] = RS.probe(CLAUDE, claude_env(), str(HOME)) or data["models"]
    data["models_checked_at"] = now()
    RS.save(RUN_SETTINGS, data)
    print("models that work: " + ", ".join(f"{x['id']} ({x['label']})" for x in data["models"]))


def cmd_pause(_):
    PAUSE.write_text(now())
    log("agent paused: no automatic runs, tickets and PRs still refresh")
    print("paused: no automatic runs, tickets and PRs still refresh")


def cmd_resume(_):
    PAUSE.unlink(missing_ok=True)
    log("agent resumed")
    print("resumed")


def cmd_doctor(_):
    def show(name, ok, detail=""):
        print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    me = TR.whoami()
    show(f"{TR.name} access", me.get("ok"), me.get("name") or me.get("error"))
    if me.get("ok"):
        try:
            show(f"{TR.name} search", True, f"{len(TR.search())} open tickets assigned to you")
        except Exception as exc:
            show(f"{TR.name} search", False, f"{type(exc).__name__}: {exc}"[:200])
    if TR.can_login:
        show("Playwright venv", VENV_PY.exists(), str(VENV_PY))
    gh_user = run([REAL_GH, "api", "user", "--jq", ".login"], check=False)
    show("gh login", gh_user.returncode == 0, gh_user.stdout.strip() or gh_user.stderr.strip()[:100])
    show("workspace", WS.exists(), str(WS))
    repos = code_repos()
    if repos:
        r = run([REAL_GIT, "-C", str(repos[0]), "ls-remote", "--exit-code", "origin", "HEAD"], check=False)
        show(f"git over ssh ({repos[0].name})", r.returncode == 0, r.stderr.strip()[:100])
    show("ticket skill", (SKILL / "SKILL.md").exists(), str(SKILL))
    show("review skill", (REVIEW_SKILL / "SKILL.md").exists(), str(REVIEW_SKILL))
    show(f"{SCHEDULER} job loaded", OS.service_loaded(SCHEDULER, LABEL), LABEL)
    show("automatic runs", not PAUSE.exists(), "paused" if PAUSE.exists() else "on")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


DESK_STOPPED = BASE / ".dashboard_stopped"


def start_desk():
    """Start the desk unless it is up. Returns its info, or None when it didn't start."""
    info = json.loads(DASH.read_text()) if DASH.exists() else {}
    if OS.desk_up(info):
        return info
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "dashboard.log", "a") as out:
        p = subprocess.Popen([sys.executable, str(BASE / "dashboard.py")], stdout=out, stderr=out,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        time.sleep(0.1)
        info = json.loads(DASH.read_text()) if DASH.exists() else {}
        if info.get("pid") == p.pid:
            return info
    return None


def cmd_dashboard(arg):
    info = json.loads(DASH.read_text()) if DASH.exists() else {}
    if arg == "stop":
        alive = OS.desk_up(info)
        if alive:
            os.kill(info["pid"], 15)
        DASH.unlink(missing_ok=True)
        # stays stopped: the scheduler only brings back a desk that went down by itself (a restart, a crash)
        DESK_STOPPED.touch()
        print("dashboard stopped" if alive else "dashboard wasn't running")
        return
    DESK_STOPPED.unlink(missing_ok=True)
    info = start_desk()
    if not info:
        sys.exit(f"dashboard didn't start, see {LOGS / 'dashboard.log'}")
    url = f"http://127.0.0.1:{info['port']}/?t={info['token']}"
    if arg != "--no-open":
        OS.open_url(url)
    print(f"dashboard: {url}\nstop it with: ptk dashboard stop")


COMMANDS = {"cycle": cmd_cycle, "list": cmd_list, "show": cmd_show, "check": cmd_check, "context": cmd_context,
            "approve": cmd_approve, "finalize": cmd_finalize, "skip": cmd_skip, "unskip": cmd_unskip,
            "login": cmd_login, "pause": cmd_pause, "resume": cmd_resume, "doctor": cmd_doctor,
            "dashboard": cmd_dashboard, "settings": cmd_settings, "models": cmd_models,
            "_check": lambda k: then_next(do_check, k), "_implement": lambda k: then_next(do_implement, k),
            "_diagnose": lambda k: do_diagnose(check_key(k)), "_tell": lambda k: do_tell(check_key(k)), "fix": cmd_fix, "rework": cmd_rework, "_repush": cmd_repush, "prs": cmd_prs}

FLAGS = take_flags()

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    try:
        COMMANDS[sys.argv[1]](sys.argv[2] if len(sys.argv) > 2 else None)
    except SignInNeeded as exc:
        tracker_status(False, error=str(exc))
        sys.exit(f"{exc}" + (". Run: ptk login" if TR.can_login else ""))
    except RuntimeError as exc:
        log(f"error: {exc}")
        sys.exit(f"error: {exc}")
