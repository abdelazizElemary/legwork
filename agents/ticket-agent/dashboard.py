#!/usr/bin/env python3
"""Local ticket desk for the ticket agent. Started by `ptk dashboard`, listens on 127.0.0.1 only."""

import atexit
import importlib.util
import json
import mimetypes
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        # skips HTTPServer's reverse DNS lookup of 127.0.0.1, which can stall for many seconds on some Macs
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / "agent-lib"))
import frame as FRAME  # noqa: E402
spec = importlib.util.spec_from_file_location("agent", BASE / "agent.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

TOKEN_FILE = BASE / ".dashboard_token"
if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(24))
    TOKEN_FILE.chmod(0o600)
TOKEN = TOKEN_FILE.read_text().strip()
LOGIN_PID = BASE / ".login.pid"


def tracker_watch():
    # keeps a browser session warm and the status line honest
    while True:
        A.tracker_status(**_whoami())
        time.sleep(120)


def signing_in():
    try:
        return A.pid_alive(int(LOGIN_PID.read_text()))
    except (OSError, ValueError):
        return False


def agent_status():
    tracker = json.loads(A.TRACKER_STATUS.read_text()) if A.TRACKER_STATUS.exists() else {"ok": None}
    return {"loaded": A.OS.service_loaded(A.SCHEDULER, A.LABEL), "polling": A.OS.lock_held(A.LOCK),
            "paused": A.PAUSE.exists(), "interval_min": A.CFG.get("interval_minutes", 10),
            "last_check": A.LAST_CYCLE.read_text().strip() if A.LAST_CYCLE.exists() else None,
            "implementing": A.implementing_now(), "tracker": tracker, "signing_in": signing_in(),
            "slots": {j: {"running": A.running_count(j), "limit": A.parallel_limit(j)} for j in A.JOBS}}


def row(st):
    a = st.get("assessment") or {}
    updated_since_pr = st.get("phase") == "pr_open" and st.get("tracker_updated_at_pr") \
        and A.same_second(st.get("tracker_updated")) != A.same_second(st.get("tracker_updated_at_pr"))
    running = A.is_running(st["key"])
    prog = A.progress.view(A.PROGRESS / f"{st['key']}.json", 1) if running else None
    if prog:
        # rounded, so the list only re-renders when something a person would notice changes
        prog = {"state": prog["state"], "current": prog["current"], "idle_s": prog["idle_s"] // 30 * 30}
    f = st.get("failure") or A.old_failure(st) or {}
    diag = st.get("diagnosis") or {}
    job = "check" if st.get("phase") == "checking" else "implement"
    return {k: st.get(k) for k in ("key", "summary", "type", "status", "priority", "sprint", "tracker_updated", "phase", "url",
                                   "error", "failed_step", "started", "state_updated", "prs")} | {
        "failure_title": (f.get("explained") or {}).get("title"), "diagnosis_state": diag.get("state"),
        "prs_live": [{"repo": p.get("repo"), "number": p.get("number"), "url": p.get("url"),
                      **{k: (p.get("live") or {}).get(k) for k in ("state", "review", "checks")}} for p in st.get("prs") or []],
        "new_feedback": st.get("new_feedback") or 0, "came_back": st.get("came_back"),
        "reworks": len(st.get("reworks") or []), "rework_mode": bool(st.get("rework_mode")), "round": st.get("round") or 1, "conflicts": any((p.get("live") or {}).get("conflicts") for p in st.get("prs") or []),
        "diagnosis_summary": diag.get("summary"),
        "run_on": A.RS.describe(*A.job_choice(st, job), run_settings_data()) if running else None,
        "running": running, "progress": prog, "verdict": a.get("verdict"),
        "questions": len(a.get("questions") or []), "updated_since_pr": bool(updated_since_pr),
        "cost": round((st.get("cost_check") or 0) + (st.get("cost_implement") or 0) + (st.get("cost_diagnose") or 0), 2)}


def build_state():
    rows = [row(s) for s in A.all_states() if not s.get("gone") or s.get("phase") in ("pr_open", "needs_attention")]
    return {"agent": agent_status(), "tickets": rows, "draft_prs": A.CFG["draft_prs"], "label": A.CFG.get("label") or A.ORG,
            "tracker": {"name": A.TR.name, "can_login": A.TR.can_login, "format": A.TR.format},
            "repos": [p.name for p in A.code_repos()],
            "budgets": {"check": A.CFG["check_budget_usd"], "implement": A.CFG["implement_budget_usd"]}}


pr_refreshing = set()


def refresh_prs_in_background(key):
    """Opening a ticket whose PRs were read more than two minutes ago re-reads them from GitHub, without
    holding up the page. One read per ticket at a time."""
    if key in pr_refreshing:
        return
    pr_refreshing.add(key)

    def work():
        try:
            A.sync_ticket_prs(key)
        except Exception as exc:
            A.log(f"{key} PR refresh: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            pr_refreshing.discard(key)
    threading.Thread(target=work, daemon=True).start()


def ticket_view(key):
    key = A.check_key(key)
    st = A.load(key) or {}
    if st.get("phase") in A.PR_PHASES and A.prs_stale(st) and not A.is_running(key):
        refresh_prs_in_background(key)
    if A.old_failure(st):
        st["failure"] = A.old_failure(st)
    d = A.TICKETS / key
    att = sorted(p.name for p in (d / "attachments").iterdir()) if (d / "attachments").is_dir() else []
    manifest = json.loads(Path(st["manifest"]).read_text()) if st.get("manifest") and Path(st["manifest"]).exists() else None
    resume = {}
    f = st.get("failure") or {}
    for label, sid in (("check", st.get("check_session")), ("implementation", st.get("implement_session")),
                       ("failed run", f.get("session_id") if f.get("session_id") not in (st.get("check_session"), st.get("implement_session")) else None)):
        if sid:
            resume[label] = f"cd {A.WS} && claude --resume {sid}"
    # issue.json is the tracker's own view for the desk: format, description, comments, links, images
    issue = json.loads((d / "issue.json").read_text()) if (d / "issue.json").exists() else {}
    log_file = A.LOGS / "agent.log"
    lines = log_file.read_text().splitlines() if log_file.exists() else []
    timeline = [l for l in lines if f" {key} " in l]
    data = run_settings_data()
    run = {j: {"override": (st.get("run_settings") or {}).get(j, {}),
               "next": A.RS.describe(*A.job_choice(st, j), data)} for j in A.JOBS}
    running = A.is_running(key)
    prog = A.progress.view(A.PROGRESS / f"{key}.json", 12) if running else None
    diag_prog = A.progress.view(A.PROGRESS / f"{key}.diagnose.json", 6) if (st.get("diagnosis") or {}).get("state") == "running" else None
    tell_prog = A.progress.view(A.PROGRESS / f"{key}.tell.json", 4) if (st.get("tell") or {}).get("state") == "thinking" else None
    if prog:
        prog["idle_s"] = prog["idle_s"] // 10 * 10
        prog["elapsed_s"] = prog["elapsed_s"] // 60 * 60
    return {"state": st, "tell_thread": A.tell_thread(st, lines), "rework_new_branches": A.rework_new_branches(),
            "attachments": att, "manifest": manifest, "resume": resume,
            "running": running, "progress": prog, "issue": issue, "timeline": timeline[-30:], "run": run,
            "diagnose_progress": diag_prog, "tell_progress": tell_prog}


probe_state = {"running": False}


def run_settings_data():
    return A.RS.load(A.RUN_SETTINGS, A.JOBS)


def run_settings_view():
    data = run_settings_data()
    u = A.RS.user_default()
    return {"defaults": data["defaults"], "models": data["models"], "efforts": A.RS.EFFORTS,
            "parallel": {j: A.parallel_limit(j) for j in A.JOBS}, "max_slots": A.MAX_SLOTS,
            "checked_at": data.get("models_checked_at"), "checking": probe_state["running"],
            "user_default": {"model": A.RS.name_of(u.get("model"), data), "effort": u.get("effort")}}


def probe_models():
    probe_state["running"] = True
    try:
        data = run_settings_data()
        data["models"] = A.RS.probe(A.CLAUDE, A.claude_env(), str(A.HOME)) or data["models"]
        data["models_checked_at"] = A.now()
        A.RS.save(A.RUN_SETTINGS, data)
    finally:
        probe_state["running"] = False


def activity():
    f = A.LOGS / "agent.log"
    return list(reversed(f.read_text().splitlines()[-40:])) if f.exists() else []


def run_ptk(*args, timeout=120):
    out = subprocess.run([sys.executable, str(BASE / "agent.py"), *args], capture_output=True, text=True, timeout=timeout)
    return out.returncode == 0, (out.stdout + out.stderr).strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "ticket-desk"

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "font-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, header_token):
        # Host check stops DNS rebinding, the token stops other pages and sites
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        if header_token:
            return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)
        t = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return secrets.compare_digest(t, TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # images can't send a header, so the attachment route takes the key in the query like the page does
        header_token = u.path.startswith("/api/") and u.path != "/api/attachment"
        if not self.allowed(header_token):
            return self.send(403, b"Open the ticket desk with: ptk dashboard", "text/plain; charset=utf-8")
        try:
            if u.path == "/":
                return self.send(200, (BASE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self.send(200, build_state())
            if u.path == "/api/ticket":
                return self.send(200, ticket_view(q.get("key", [""])[0]))
            if u.path == "/api/frame":
                return self.send(200, FRAME.frame(BASE.parent, BASE.parent / "agent-lib", "ticket-agent"))
            if u.path == "/api/activity":
                return self.send(200, {"lines": activity()})
            if u.path == "/api/run-settings":
                return self.send(200, run_settings_view())
            if u.path == "/api/attachment":
                key = A.check_key(q.get("key", [""])[0])
                name = q.get("name", [""])[0]
                folder = A.TICKETS / key / "attachments"
                if not folder.is_dir() or name not in os.listdir(folder):
                    return self.send(404, {"error": "no such attachment"})
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if not ctype.startswith("image/") or ctype == "image/svg+xml":
                    ctype = "application/octet-stream"
                return self.send(200, (folder / name).read_bytes(), ctype)
        except SystemExit as exc:
            return self.send(400, {"error": str(exc)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self.allowed(True) or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(403, {"error": "forbidden"})
        size = int(self.headers.get("Content-Length") or 0)
        if size > (16_000_000 if u.path == "/api/upload" else 1_000_000):
            return self.send(413, {"ok": False, "message": "too big"})
        body = json.loads(self.rfile.read(size) or b"{}")
        key = body.get("key", "")
        try:
            if u.path == "/api/check":
                k = A.check_key(key)
                st = A.known(k)
                if A.is_running(k):
                    ok, msg = False, "the agent is already working on it"
                else:
                    A.set_phase(st, "check_queued", error=None)
                    A.start_queued()
                    free = A.parallel_limit("check") - A.running_count("check")
                    ok, msg = True, "check started" if free > 0 else "queued, it starts when a check slot frees up"
            elif u.path == "/api/context":
                text = (body.get("text") or "").strip()
                if not text:
                    ok, msg = False, "write the context first"
                else:
                    args = ["context", key, text] + (["--implement"] if body.get("implement") else [])
                    ok, msg = run_ptk(*args)
            elif u.path == "/api/approve":
                args = ["approve", key] + ([body["notes"]] if (body.get("notes") or "").strip() else [])
                ok, msg = run_ptk(*args)
            elif u.path == "/api/finalize":
                k = A.check_key(key)
                A.spawn("finalize", k)
                ok, msg = True, "PR step started"
            elif u.path == "/api/rerun":
                k = A.check_key(key)
                st = A.load(k) or {}
                ok, msg = True, A.request_fix(k, "", "rerun_pr_step" if st.get("failed_step") == "pr" else "rerun")
            elif u.path == "/api/fix":
                action = body.get("action") or "resume"
                if action not in ("resume", "rerun", "rerun_pr_step"):
                    raise SystemExit("action must be resume, rerun or rerun_pr_step")
                ok, msg = True, A.request_fix(A.check_key(key), body.get("instructions") or "", action)
            elif u.path == "/api/prs/refresh":
                # one ticket when a key is sent (the PR card), every ticket otherwise (the header button)
                n = int(A.sync_ticket_prs(A.check_key(key))) if key else A.sync_prs()
                ok, msg = True, f"PR status refreshed for {n} {'ticket' if n == 1 else 'tickets'}."
            elif u.path == "/api/upload":
                import base64
                k = A.check_key(key)
                A.known(k)
                ok, msg = True, A.save_attachment(k, body.get("name") or "image", base64.b64decode((body.get("data") or "").split(",", 1)[-1]))
            elif u.path == "/api/tell":
                k = A.check_key(key)
                A.known(k)
                ok, msg = True, A.request_tell(k, body.get("message") or "", body.get("images") or [])
            elif u.path == "/api/tell/do":
                ok, msg = True, A.apply_tell(A.check_key(key), body.get("id") or "")
            elif u.path == "/api/tell/dismiss":
                ok, msg = True, A.dismiss_tell(A.check_key(key))
            elif u.path == "/api/conflict":
                ok, msg = True, A.request_conflict_fix(A.check_key(key), body.get("instructions") or "")
            elif u.path == "/api/rework":
                ok, msg = True, A.request_rework(A.check_key(key), body.get("instructions") or "", body.get("images") or [])
            elif u.path == "/api/diagnose":
                k = A.check_key(key)
                st = A.known(k)
                if not st.get("failure"):
                    ok, msg = False, "this ticket has no failure to explain"
                else:
                    st["diagnosis"] = None
                    A.save(st)
                    A.spawn("_diagnose", k)
                    ok, msg = True, "Looking into it again."
            elif u.path == "/api/parallel":
                data = run_settings_data()
                par = data.setdefault("parallel", {})
                for j in A.JOBS:
                    if body.get(j) is not None:
                        par[j] = max(1, min(A.MAX_SLOTS, int(body[j])))
                A.RS.save(A.RUN_SETTINGS, data)
                A.start_queued()
                ok, msg = True, f"Up to {par.get('check', A.parallel_limit('check'))} checks and {par.get('implement', A.parallel_limit('implement'))} implementations run at once."
            elif u.path in ("/api/skip", "/api/unskip"):
                ok, msg = run_ptk(u.path.rsplit("/", 1)[1], key)
            elif u.path == "/api/agent":
                action = body.get("action")
                if action in ("pause", "resume"):
                    ok, msg = run_ptk(action)
                elif action == "poll":
                    ok, err = A.OS.service_kick(A.SCHEDULER, A.LABEL, [sys.executable, str(BASE / "agent.py"), "cycle"])
                    msg = err or f"checking {A.TR.name}"
                else:
                    ok, msg = False, "unknown action"
            elif u.path == "/api/run-settings":
                job = body.get("job")
                if job not in A.JOBS:
                    raise SystemExit("job must be check or implement")
                data = run_settings_data()
                m, e = A.RS.clean(body.get("model"), body.get("effort"), data)
                data["defaults"][job] = {"model": m, "effort": e}
                A.RS.save(A.RUN_SETTINGS, data)
                ok, msg = True, f"{dict(check='Checks', implement='Implementations', diagnose='Diagnoses')[job]} now run on {A.RS.describe(m, e, data)}."
            elif u.path == "/api/job-settings":
                k, job = A.check_key(key), body.get("job")
                if job not in A.JOBS:
                    raise SystemExit("job must be check or implement")
                st = A.known(k)
                entry = A.set_job_override(st, job, body.get("model"), body.get("effort"))
                A.touch_user(st)
                A.save(st)
                what = "check" if job == "check" else "implementation"
                ok, msg = True, (f"This ticket's {what} now runs on {A.RS.describe(entry.get('model'), entry.get('effort'), run_settings_data())}."
                                 if entry else f"This ticket's {what} uses the default again.")
            elif u.path == "/api/models/check":
                if not probe_state["running"]:
                    threading.Thread(target=probe_models, daemon=True).start()
                ok, msg = True, "Checking which models work, it takes about a minute."
            elif u.path == "/api/login":
                if not A.TR.can_login:
                    ok, msg = False, f"{A.TR.name} has no browser sign-in, see the status line"
                elif signing_in():
                    ok, msg = True, "the sign-in window is already open"
                else:
                    with open(A.LOGS / "login.log", "a") as out:
                        p = subprocess.Popen([str(A.VENV_PY), str(BASE / "trackers" / "jira.py"), "login"], stdout=out,
                                             stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
                    LOGIN_PID.write_text(str(p.pid))
                    threading.Thread(target=lambda: (p.wait(), A.tracker_status(**_whoami())), daemon=True).start()
                    ok, msg = True, "sign-in window opened"
            else:
                return self.send(404, {"error": "not found"})
        except SystemExit as exc:
            ok, msg = False, str(exc)
        except Exception as exc:
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        self.send(200 if ok else 400, {"ok": ok, "message": msg})


def _whoami():
    me = A.TR.whoami()
    return {"ok": bool(me.get("ok")), "name": me.get("name"), "error": me.get("error")}


def main():
    port = A.CFG.get("dashboard_port", 8788)
    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError:
        server = LocalServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    A.DASH.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": TOKEN, "started": A.now()}))
    A.DASH.chmod(0o600)
    atexit.register(lambda: A.DASH.unlink(missing_ok=True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=tracker_watch, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
