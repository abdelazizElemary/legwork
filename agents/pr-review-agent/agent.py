#!/usr/bin/env python3
"""Unattended PR review agent. It drafts a review for every new review request and never posts.

prr cycle            one poll, what the scheduler runs every few minutes
prr list             drafts waiting for you, failures, recent posts
prr show <pr>        the draft: verdict, body and every comment
prr post <pr>        re-check the draft against the PR, then post it
prr skip <pr>        drop it until the PR gets new commits
prr review <pr>      draft a review now, whatever the state
prr baseline         mark every pending request as seen
prr pause | resume   stop or restart the automatic runs
prr clean            remove the agent's review worktrees of closed PRs
prr doctor           check gh, git, claude and the scheduler
prr dashboard [stop] open the local dashboard in the browser, or stop it
prr settings [--model M] [--effort E]   show or change the model and effort reviews use
prr review <pr> --model M --effort E    the same, for one PR
prr models           re-check which models your Claude setup allows
<pr> is a PR URL, org/repo#N or repo#N.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BASE = Path(__file__).resolve().parent
LIB = BASE.parent / "agent-lib"
sys.path.insert(0, str(LIB))
import osglue as OS  # noqa: E402
import runsettings as RS  # noqa: E402
import progress  # noqa: E402

STATE, LOGS = BASE / "state", BASE / "logs"
PAUSE, LOCK, INIT = BASE / "paused", BASE / ".lock", BASE / "state" / ".initialized"
LAST_CYCLE, DASH = BASE / "last_cycle", BASE / "dashboard.json"
RUN_SETTINGS, OVERRIDES = BASE / "run_settings.json", BASE / "overrides.json"
PROGRESS = BASE / "progress"
CFG = json.loads((BASE / "config.json").read_text())
SKILL = Path(os.path.expanduser(CFG.get("skill_dir") or str(BASE.parent / "skills" / "pr-review")))
LABEL, SCHEDULER = CFG["service_label"], CFG.get("scheduler", "none")
CLAUDE, REAL_GH = os.path.expanduser(CFG["claude_bin"]), os.path.expanduser(CFG["gh_bin"])
HOST, AUTHOR = CFG["host"], CFG["author"]["name"]
WS = {org: Path(os.path.expanduser(p)) for org, p in CFG["workspaces"].items()}
RUN_PATH = ":".join(os.path.expanduser(p) for p in CFG["path"].split(":"))
CRED_VARS = CFG.get("shell_env", [])
# the skill's own scripts are allowed: posting is already refused by PR_REVIEW_NO_POST and the gh shim
SCRIPTS = [f"{root}/scripts/{s}" for root in (str(SKILL), "~/.claude/skills/pr-review")
           for s in ("pr_worktree.sh", "diff_coverage.py", "post_review.py")]
DENY = {"permissions": {
    "allow": [f"Bash({runner} {s}:*)" for s in SCRIPTS for runner in ("bash", "python3")],
    "deny": ["Bash(git push:*)", "Bash(gh pr review:*)", "Bash(gh pr merge:*)",
             "Bash(gh pr comment:*)", "Bash(gh pr close:*)"]}}


def check_draft(draft):
    """Run post_review.py in check-only mode, so a draft's checks never depend on the model running them."""
    out = subprocess.run([sys.executable, str(SKILL / "scripts" / "post_review.py"), str(draft)],
                         capture_output=True, text=True, env=claude_env({"PR_REVIEW_NO_POST": "1", "GH_HOST": HOST}))
    lines = [l for l in out.stdout.splitlines() if l.startswith(("ERROR", "warning"))]
    return out.returncode == 0, lines


def now():
    return time.strftime("%Y-%m-%d %H:%M")


def log(msg):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "agent.log", "a") as f:
        f.write(f"{now()} {msg}\n")


def gh(*args, check=True):
    env = {**os.environ, "GH_HOST": HOST, "PATH": RUN_PATH}
    out = subprocess.run([REAL_GH, *args], capture_output=True, text=True, env=env)
    if check and out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {out.stderr.strip()[:300]}")
    return out.stdout


def claude_env(extra=None):
    return OS.claude_env(CRED_VARS, HOME, RUN_PATH, extra)


def notify(title, text):
    if CFG.get("notify", True):
        OS.notify(title, text)


def key_of(org, repo, n):
    return f"{org}/{repo}#{n}"


def state_file(org, repo, n):
    return STATE / f"{org}__{repo}__{n}.json"


def load(org, repo, n):
    f = state_file(org, repo, n)
    return json.loads(f.read_text()) if f.exists() else None


def save(st):
    STATE.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = now()
    f = state_file(st["org"], st["repo"], st["number"])
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(f)


def all_states():
    return [json.loads(f.read_text()) for f in sorted(STATE.glob("*.json"))]


def parse_pr(arg):
    m = re.search(r"/([^/]+)/([^/]+)/pull/(\d+)", arg)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    m = re.fullmatch(r"(?:([^/#\s]+)/)?([^/#\s]+)#(\d+)", arg.strip())
    if not m:
        sys.exit(f"can't read PR '{arg}', use a URL, org/repo#N or repo#N")
    org, repo, n = m.group(1), m.group(2), int(m.group(3))
    if not org:
        hits = [o for o, ws in WS.items() if (ws / repo / ".git").exists()]
        hits += [s["org"] for s in all_states() if s["repo"] == repo and s["org"] not in hits]
        if len(hits) != 1:
            sys.exit(f"can't tell the org of {repo}, use org/{repo}#{n}")
        org = hits[0]
    if org not in WS:
        sys.exit(f"{org} isn't one of the review agent's orgs: {', '.join(WS)}")
    return org, repo, n


def pending():
    out = []
    for org in WS:
        rows = json.loads(gh("search", "prs", "--review-requested=@me", "--state=open", "--owner", org,
                             "--limit", "100", "--json", "repository,number,title,author,url,isDraft"))
        for r in rows:
            login = r["author"]["login"]
            if r["isDraft"] or login.endswith("[bot]") or login.startswith("app/") or login in CFG["skip_authors"]:
                continue
            out.append({"org": org, "repo": r["repository"]["name"], "number": r["number"],
                        "title": r["title"], "author": login, "url": r["url"]})
    return out


def head_of(org, repo, n):
    return gh("api", f"repos/{org}/{repo}/pulls/{n}", "--jq", ".head.sha").strip()


def local_repo(org, repo):
    ws = WS[org]
    d = ws / repo
    if (d / ".git").exists():
        return d
    d = ws / ".local" / "clones" / repo
    if not (d / ".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", f"git@{HOST}:{org}/{repo}.git", str(d)], check=True,
                       env={**os.environ, "PATH": RUN_PATH})
    return d


def progress_file(org, repo, n):
    return PROGRESS / f"{org}__{repo}__{n}.json"


def pr_lock(org, repo, n):
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / f"{org}__{repo}__{n}.lock"


def is_running(org, repo, n):
    return OS.lock_held(pr_lock(org, repo, n))


def overrides():
    return json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}


def set_override(key, model=None, effort=None):
    """Per-PR model and effort. None clears that part back to the default."""
    data = RS.load(RUN_SETTINGS, ["review"])
    model, effort = RS.clean(model, effort, data)
    o = overrides()
    entry = {k: v for k, v in (("model", model), ("effort", effort)) if v}
    if entry:
        o[key] = entry
    else:
        o.pop(key, None)
    tmp = OVERRIDES.with_suffix(".tmp")
    tmp.write_text(json.dumps(o, indent=1))
    tmp.replace(OVERRIDES)
    return entry


def run_choice(key):
    return RS.resolve(RS.load(RUN_SETTINGS, ["review"]), "review", overrides().get(key))


def run_review(pr, head=None):
    org, repo, n = pr["org"], pr["repo"], pr["number"]
    # one review per PR at a time, the cycle and a manual trigger can race
    lock = open(pr_lock(org, repo, n), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"{key_of(org, repo, n)} is already being reviewed")
        return load(org, repo, n) or {"status": "running"}
    ws = WS[org]
    head = head or head_of(org, repo, n)
    st = load(org, repo, n) or {}
    attempts = st.get("attempts", 0) + 1 if st.get("head") == head else 1
    st.update({k: pr[k] for k in ("org", "repo", "number", "title", "author", "url") if k in pr})
    st.update({"head": head, "status": "running", "attempts": attempts, "error": None})
    save(st)
    draft = ws / ".local" / "reviews" / f"{repo}-pr{n}.json"
    run_log = LOGS / f"{org}__{repo}__{n}__{head[:7]}.json"
    started = time.time()
    try:
        repo_dir = local_repo(org, repo)
        prompt = (
            f"/pr-review {pr['url']} draft\n\n"
            f"This run is unattended. The PR review agent started it because {AUTHOR} was requested as a reviewer, "
            f"and nobody is watching. Local repo: {repo_dir}. Never post, reply, push or ask questions "
            "(GitHub writes are blocked anyway). Write the draft to "
            f"{draft} as the skill says, even for a clean approve, so it can be posted with one command. "
            "What gets posted follows references/policy.md. Finish with the usual terminal "
            "summary (minor points under Not posted:), then one last line exactly like, where minor counts "
            'the Not posted notes:\nRESULT {"verdict": "APPROVE", "blocking": 0, "minor": 2}'
        )
        env = claude_env({"PATH": f"{LIB / 'shims'}:{RUN_PATH}", "GH_HOST": HOST, "PR_REVIEW_NO_POST": "1"})
        cmd = [CLAUDE, "-p", prompt, "--permission-mode", "auto", "--permission-prompts", "none",
               "--output-format", "json", "--max-budget-usd", str(CFG["max_budget_usd"]),
               "--settings", json.dumps(DENY), "--add-dir", "/tmp"]
        model, effort = run_choice(key_of(org, repo, n))
        cmd += RS.flags(model, effort)
        st.update({"run_effort": effort or RS.user_default().get("effort")})
        # streamed, so the dashboard can show each step while it runs
        # the review follows the PR's model and effort live: a change on the desk switches it within seconds
        res, stderr = progress.run_claude(cmd, ws, env, CFG["review_timeout_minutes"] * 60,
                                          progress_file(org, repo, n), "review",
                                          desired=lambda: RS.concrete(*run_choice(key_of(org, repo, n))),
                                          describe=lambda m, e: RS.describe(m, e, RS.load(RUN_SETTINGS, ["review"])))
        LOGS.mkdir(parents=True, exist_ok=True)
        run_log.write_text(json.dumps(res) if res else stderr)
        text = res.get("result", "") or ""
        m = re.search(r"^RESULT\s+(\{.*\})\s*$", text, re.M)
        result = json.loads(m.group(1)) if m else {}
        fresh = draft.exists() and draft.stat().st_mtime >= started
        verdict = json.loads(draft.read_text()).get("event") if fresh else result.get("verdict")
        used = next(iter((res.get("modelUsage") or {}).keys()), None)
        st.update({"run_model": RS.label(used) if used else RS.name_of(model or RS.user_default().get("model"))})
        st.update({"session_id": res.get("session_id"), "cost_usd": res.get("total_cost_usd"),
                   "log": str(run_log), "summary": re.sub(r"\n?RESULT\s+\{.*\}\s*$", "", text).strip(),
                   "blocking": result.get("blocking"), "minor": result.get("minor"), "verdict": verdict,
                   "draft": str(draft) if fresh else None, "workspace": str(ws)})
        if res.get("is_error") or not fresh:
            st.update({"status": "failed",
                       "error": (text or stderr or "no draft written")[-400:]})
            notify(f"PR review failed: {repo}#{n}", (st["error"] or "")[:150])
        else:
            ok, issues = check_draft(draft)
            st.update({"status": "drafted", "checks_ok": ok, "check_issues": issues})
            counts = f"{st['blocking'] or 0} blocking" if verdict == "REQUEST_CHANGES" else "no comments"
            flag = "" if ok else " Checks failed, see prr show."
            notify(f"{verdict}: {repo}#{n}", f"{pr.get('title', '')[:80]} ({counts}).{flag} prr show {repo}#{n}")
    except subprocess.TimeoutExpired:
        st.update({"status": "failed", "error": f"timed out after {CFG['review_timeout_minutes']} min"})
        notify(f"PR review timed out: {repo}#{n}", "prr review to retry")
    except Exception as exc:
        st.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:400]})
        notify(f"PR review failed: {repo}#{n}", st["error"][:150])
    save(st)
    log(f"{key_of(org, repo, n)} {head[:7]} {st['status']} {st.get('verdict') or ''} {st.get('error') or ''}".strip())
    return st


def cmd_cycle(_):
    # after a restart nothing else starts the desk
    if not DESK_STOPPED.exists():
        start_desk()
    if PAUSE.exists():
        return
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    LAST_CYCLE.write_text(now())
    if not INIT.exists():
        cmd_baseline(None)
        return
    queue = []
    for pr in pending():
        st = load(pr["org"], pr["repo"], pr["number"])
        head = head_of(pr["org"], pr["repo"], pr["number"])
        if st and st.get("head") == head:
            retry = st["status"] in ("failed", "running") and st.get("attempts", 0) < CFG["max_attempts_per_commit"]
            if not retry:
                continue
        queue.append((pr, head))
    for pr, head in queue[:CFG["max_reviews_per_cycle"]]:
        log(f"reviewing {key_of(pr['org'], pr['repo'], pr['number'])} {head[:7]}")
        run_review(pr, head)


def cmd_baseline(_):
    n = 0
    for pr in pending():
        st = load(pr["org"], pr["repo"], pr["number"]) or {}
        st.update({**pr, "head": head_of(pr["org"], pr["repo"], pr["number"]), "status": "seen"})
        save(st)
        n += 1
    STATE.mkdir(parents=True, exist_ok=True)
    INIT.write_text(now())
    log(f"baseline: {n} pending requests marked as seen")
    print(f"marked {n} pending review requests as seen; only new ones get reviewed")


def cmd_list(_):
    rows = all_states()
    waiting = [s for s in rows if s["status"] == "drafted"]
    failed = [s for s in rows if s["status"] in ("failed", "running")]
    posted = [s for s in rows if s["status"] == "posted"][-5:]
    print("paused\n" if PAUSE.exists() else "", end="")
    for title, group in (("waiting for you", waiting), ("failed", failed), ("posted recently", posted)):
        if not group:
            continue
        print(title)
        for s in group:
            extra = s.get("error") if s["status"] in ("failed", "running") else \
                f"{s.get('verdict')}" + (f", {s['blocking']} blocking" if s.get("blocking") else "") \
                + (f", {s['minor']} notes not posted" if s.get("minor") else "") \
                + ("" if s.get("checks_ok", True) else ", checks FAILED")
            print(f"  {s['repo']}#{s['number']}  {s.get('author', '')}  {s.get('title', '')[:60]}\n      {extra}  ({s['updated_at']})")
    if not (waiting or failed or posted):
        print("nothing waiting")


def find(arg):
    org, repo, n = parse_pr(arg)
    st = load(org, repo, n)
    if not st:
        sys.exit(f"no review for {repo}#{n} yet, run: prr review {arg}")
    return st


def cmd_show(arg):
    st = find(arg)
    print(f"{st['repo']}#{st['number']} by {st.get('author')}: {st.get('title')}\n{st.get('url')}\n"
          f"status {st['status']}, head {st['head'][:7]}, cost ${st.get('cost_usd') or 0:.2f}"
          + (f" on {st['run_model']} at {st.get('run_effort')} effort" if st.get("run_model") else "") + "\n")
    if st.get("draft") and Path(st["draft"]).exists():
        d = json.loads(Path(st["draft"]).read_text())
        print(f"{d.get('event')}\n\n{d.get('body') or '(empty body)'}\n")
        for c in d.get("comments", []):
            tag = "blocking" if c.get("blocking") else "minor"
            print(f"--- {c['path']}:{c['line']} [{tag}]\n{c['body']}\n")
        for t in d.get("resolve_threads") or []:
            print(f"--- resolves {t.get('path')}:{t.get('line')}: {t.get('comment') or ''}\n  fixed: {(t.get('evidence') or [''])[0]}\n")
    if st.get("check_issues") is not None:
        print("--- posting checks: " + ("passed" if st.get("checks_ok") else "FAILED"))
        for line in st["check_issues"]:
            print(f"  {line}")
        print()
    if st.get("summary"):
        print(f"--- summary\n{st['summary']}\n")
    if st.get("error"):
        print(f"--- error\n{st['error']}\n")
    if st.get("session_id"):
        print(f"to talk it through: cd {st.get('workspace')} && claude --resume {st['session_id']}")


def cmd_post(arg):
    st = find(arg)
    if st["status"] != "drafted" or not st.get("draft"):
        sys.exit(f"{st['repo']}#{st['number']} has no draft to post (status {st['status']})")
    script = str(SKILL / "scripts" / "post_review.py")
    env = {k: v for k, v in os.environ.items() if k != "PR_REVIEW_NO_POST"}
    env.update({"GH_HOST": HOST, "PATH": RUN_PATH})
    out = subprocess.run([sys.executable, script, st["draft"], "--post"], capture_output=True, text=True, env=env)
    print(out.stdout.strip() or out.stderr.strip())
    if out.returncode == 0:
        m = re.search(r"posted \S+: (\S+)", out.stdout)
        st.update({"status": "posted", "review_url": m.group(1) if m else None})
        save(st)
    else:
        sys.exit(1)


def cmd_skip(arg):
    st = find(arg)
    st["status"] = "skipped"
    save(st)
    print(f"skipped {st['repo']}#{st['number']} until it gets new commits")


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


def cmd_review(arg):
    org, repo, n = parse_pr(arg)
    if FLAGS:
        set_override(key_of(org, repo, n), FLAGS.get("model"), FLAGS.get("effort"))
    pull = json.loads(gh("api", f"repos/{org}/{repo}/pulls/{n}"))
    pr = {"org": org, "repo": repo, "number": n, "title": pull["title"], "author": pull["user"]["login"],
          "url": pull["html_url"]}
    m, e = run_choice(key_of(org, repo, n))
    print(f"reviewing {repo}#{n} in draft mode on {RS.describe(m, e, RS.load(RUN_SETTINGS, ['review']))}, this can take a while")
    st = run_review(pr, pull["head"]["sha"])
    print(f"{st['status']}: {st.get('verdict') or st.get('error')}\nprr show {repo}#{n}")


def cmd_settings(_):
    """prr settings [--model X] [--effort Y]: show or change the default for reviews."""
    data = RS.load(RUN_SETTINGS, ["review"])
    if FLAGS:
        m, e = RS.clean(FLAGS.get("model"), FLAGS.get("effort"), data)
        if FLAGS.get("model") not in (None, "default") and not m:
            sys.exit(f"model must be one of: {', '.join(x['id'] for x in data['models'])} (prr models re-checks)")
        if FLAGS.get("effort") not in (None, "default") and not e:
            sys.exit(f"effort must be one of: {', '.join(RS.EFFORTS)}")
        d = data["defaults"]["review"]
        if FLAGS.get("model") is not None:
            d["model"] = None if FLAGS["model"] == "default" else m
        if FLAGS.get("effort") is not None:
            d["effort"] = None if FLAGS["effort"] == "default" else e
        RS.save(RUN_SETTINGS, data)
    d = data["defaults"]["review"]
    print(f"reviews run on {RS.describe(d.get('model'), d.get('effort'), data)}")
    print("models that work: " + (", ".join(f"{x['id']} ({x['label']})" for x in data["models"]) or "not checked yet, run prr models"))
    print("efforts: " + ", ".join(RS.EFFORTS))
    for k, v in overrides().items():
        print(f"  {k}: {RS.describe(v.get('model'), v.get('effort'), data)}")


def cmd_models(_):
    data = RS.load(RUN_SETTINGS, ["review"])
    print("checking which models your Claude setup allows, a few cents each")
    data["models"] = RS.probe(CLAUDE, claude_env(), str(HOME)) or data["models"]
    data["models_checked_at"] = now()
    RS.save(RUN_SETTINGS, data)
    print("models that work: " + ", ".join(f"{x['id']} ({x['label']})" for x in data["models"]))


def cmd_pause(_):
    PAUSE.write_text(now())
    print("paused, prr resume to restart")


def cmd_resume(_):
    PAUSE.unlink(missing_ok=True)
    print("resumed")


def cmd_clean(_):
    removed = 0
    for st in all_states():
        if st["status"] not in ("posted", "skipped", "seen", "drafted", "failed"):
            continue
        state = gh("api", f"repos/{st['org']}/{st['repo']}/pulls/{st['number']}", "--jq", ".state", check=False).strip()
        if state != "closed":
            continue
        wt = WS[st["org"]] / ".local" / "wt" / f"{st['repo']}-pr{st['number']}"
        if not (wt / ".git").is_file():
            continue
        common = subprocess.run(["git", "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True).stdout.strip()
        main = Path(common).parent
        if subprocess.run(["git", "-C", str(main), "worktree", "remove", "--force", str(wt)]).returncode == 0:
            removed += 1
            print(f"removed {wt}")
    print(f"removed {removed} worktree(s) of closed PRs")


def cmd_doctor(_):
    def check(name, ok, detail=""):
        print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    try:
        check("gh login", True, gh("api", "user", "--jq", ".login").strip())
    except RuntimeError as exc:
        check("gh login", False, str(exc))
    for org, ws in WS.items():
        check(f"workspace {org}", ws.exists(), str(ws))
        sample = next((p for p in sorted(ws.iterdir()) if (p / ".git").exists()), None) if ws.exists() else None
        if sample:
            r = subprocess.run(["git", "-C", str(sample), "ls-remote", "--exit-code", "origin", "HEAD"],
                               capture_output=True, text=True, env={**os.environ, "PATH": RUN_PATH})
            check(f"git over ssh ({sample.name})", r.returncode == 0, r.stderr.strip()[:120])
    check("skill", (SKILL / "SKILL.md").exists(), str(SKILL))
    refs = SKILL / "references" / "projects.md"
    check("project index", refs.exists(), str(refs))
    for tool in ("node", "npm", "uv", "python3", "jq"):
        found = any((Path(p) / tool).exists() for p in RUN_PATH.split(":"))
        check(f"{tool} on the run PATH", found)
    # no --model here: some setups only allow the default model
    r = subprocess.run([CLAUDE, "-p", "Reply with exactly: ok", "--output-format", "json",
                        "--max-budget-usd", "1"], capture_output=True, text=True, timeout=180,
                       cwd=str(HOME), env=claude_env())
    res = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
    check("claude headless", res.get("result", "").strip().lower().startswith("ok"), (res.get("result") or r.stderr)[:120])
    check(f"{SCHEDULER} job loaded", OS.service_loaded(SCHEDULER, LABEL), LABEL)
    check("automatic runs", not PAUSE.exists(), "paused" if PAUSE.exists() else "on")


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
    print(f"dashboard: {url}\nstop it with: prr dashboard stop")


COMMANDS = {"cycle": cmd_cycle, "list": cmd_list, "show": cmd_show, "post": cmd_post, "skip": cmd_skip,
            "review": cmd_review, "baseline": cmd_baseline, "pause": cmd_pause, "resume": cmd_resume,
            "clean": cmd_clean, "doctor": cmd_doctor, "dashboard": cmd_dashboard,
            "settings": cmd_settings, "models": cmd_models}
FLAGS = take_flags()

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    needs_arg = sys.argv[1] in ("show", "post", "skip", "review")
    if needs_arg and len(sys.argv) < 3:
        sys.exit(f"usage: prr {sys.argv[1]} <pr>")
    try:
        COMMANDS[sys.argv[1]](sys.argv[2] if len(sys.argv) > 2 else None)
    except RuntimeError as exc:
        log(f"error: {exc}")
        sys.exit(f"error: {exc}")
