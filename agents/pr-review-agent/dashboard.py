#!/usr/bin/env python3
"""Local dashboard for the PR review agent. Started by `prr dashboard`, listens on 127.0.0.1 only."""

import atexit
import importlib.util
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
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

LABELS = A.CFG.get("labels", {})
TOKEN_FILE = BASE / ".dashboard_token"
if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(24))
    TOKEN_FILE.chmod(0o600)
TOKEN = TOKEN_FILE.read_text().strip()
GQL = """query($q:String!){search(query:$q,type:ISSUE,first:100){nodes{... on PullRequest{
number title url isDraft headRefOid updatedAt createdAt additions deletions changedFiles
author{login __typename} repository{name owner{login}}}}}}"""

cache = {"prs": [], "fetched_at": None, "error": None}
cache_lock = threading.Lock()


def fetch_requests():
    q = "is:pr is:open archived:false review-requested:@me " + " ".join(f"org:{o}" for o in A.WS)
    try:
        nodes = json.loads(A.gh("api", "graphql", "-f", f"query={GQL}", "-f", f"q={q}"))["data"]["search"]["nodes"]
        prs = []
        for n in nodes:
            author = n.get("author") or {}
            if author.get("__typename") == "Bot" or author.get("login", "").endswith("[bot]") \
                    or author.get("login") in A.CFG["skip_authors"]:
                continue
            prs.append({"org": n["repository"]["owner"]["login"], "repo": n["repository"]["name"],
                        "number": n["number"], "title": n["title"], "url": n["url"], "author": author.get("login"),
                        "head": n["headRefOid"], "is_draft": n["isDraft"], "updated": n["updatedAt"],
                        "additions": n["additions"], "deletions": n["deletions"], "files": n["changedFiles"]})
        with cache_lock:
            cache.update(prs=prs, fetched_at=time.time(), error=None)
    except Exception as exc:
        with cache_lock:
            cache.update(error=f"{type(exc).__name__}: {exc}"[:300], fetched_at=time.time())


def refresher():
    while True:
        fetch_requests()
        time.sleep(180)


def agent_status():
    # a running cycle holds the agent's lock, which works the same under launchd, systemd and cron
    return {"loaded": A.OS.service_loaded(A.SCHEDULER, A.LABEL), "checking": A.OS.lock_held(A.LOCK),
            "paused": A.PAUSE.exists(), "interval_min": A.CFG.get("interval_minutes", 10),
            "last_check": A.LAST_CYCLE.read_text().strip() if A.LAST_CYCLE.exists() else None}


def row_state(live, st, running):
    if running:
        return "reviewing"
    if live and live["is_draft"] and not st:
        return "draft_pr"
    if st is None:
        return "queued"
    if live and st.get("head") != live["head"]:
        return "new_commits"
    return {"seen": "not_reviewed", "drafted": "ready", "failed": "failed", "running": "stopped",
            "posted": "posted", "skipped": "skipped"}.get(st.get("status"), "not_reviewed")


def recent(st, days=7):
    try:
        return datetime.strptime(st.get("updated_at", ""), "%Y-%m-%d %H:%M") > datetime.now() - timedelta(days=days)
    except ValueError:
        return False


def build_state():
    with cache_lock:
        live = {A.key_of(p["org"], p["repo"], p["number"]): p for p in cache["prs"]}
        gh_info = {"fetched_at": cache["fetched_at"], "error": cache["error"]}
    states = {A.key_of(s["org"], s["repo"], s["number"]): s for s in A.all_states()}
    keys = set(live) | {k for k, s in states.items()
                        if s.get("status") in ("drafted", "running", "failed") or (s.get("status") == "posted" and recent(s))}
    orgs = {o: {"org": o, "label": LABELS.get(o, o), "rows": [], "spent_week": 0.0} for o in A.WS}
    for s in states.values():
        if s["org"] in orgs and recent(s):
            orgs[s["org"]]["spent_week"] += s.get("cost_usd") or 0
    for k in keys:
        p, s = live.get(k), states.get(k)
        src = p or s
        if src["org"] not in orgs:
            continue
        running = A.is_running(src["org"], src["repo"], src["number"])
        prog = A.progress.view(A.progress_file(src["org"], src["repo"], src["number"]), 1) if running else None
        if prog:
            # rounded, so the list only re-renders when something a person would notice changes
            prog = {"state": prog["state"], "current": prog["current"], "idle_s": prog["idle_s"] // 30 * 30}
        orgs[src["org"]]["rows"].append({
            "progress": prog,
            "key": k, "org": src["org"], "repo": src["repo"], "number": src["number"],
            "title": src.get("title"), "author": src.get("author"), "url": src.get("url"),
            "state": row_state(p, s, running), "requested": bool(p),
            "verdict": (s or {}).get("verdict"), "blocking": (s or {}).get("blocking"),
            "minor": (s or {}).get("minor"), "checks_ok": (s or {}).get("checks_ok", True),
            "error": (s or {}).get("error"), "review_url": (s or {}).get("review_url"),
            "cost": (s or {}).get("cost_usd"), "state_updated": (s or {}).get("updated_at"),
            "override": A.overrides().get(k), "run_model": (s or {}).get("run_model"), "run_effort": (s or {}).get("run_effort"),
            "updated": (p or {}).get("updated"), "additions": (p or {}).get("additions"),
            "deletions": (p or {}).get("deletions"), "files": (p or {}).get("files")})
    return {"agent": agent_status(), "github": gh_info, "orgs": list(orgs.values()), "host": A.HOST}


def draft_view(key):
    org, repo, n = A.parse_pr(key)
    st = A.load(org, repo, n) or {}
    d = json.loads(Path(st["draft"]).read_text()) if st.get("draft") and Path(st["draft"]).exists() else None
    resume = f"cd {st['workspace']} && claude --resume {st['session_id']}" if st.get("session_id") else None
    key = A.key_of(org, repo, n)
    return {"state": st, "draft": d, "resume": resume, "running": A.is_running(org, repo, n),
            "progress": A.progress.view(A.progress_file(org, repo, n), 12),
            "override": A.overrides().get(key, {}), "next_run": A.RS.describe(*A.run_choice(key), run_settings_data())}


probe_state = {"running": False}


def run_settings_data():
    return A.RS.load(A.RUN_SETTINGS, ["review"])


def run_settings_view():
    data = run_settings_data()
    u = A.RS.user_default()
    return {"defaults": data["defaults"], "models": data["models"], "efforts": A.RS.EFFORTS,
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


def run_prr(*args, timeout=180):
    out = subprocess.run([sys.executable, str(BASE / "agent.py"), *args], capture_output=True, text=True,
                         timeout=timeout, env={k: v for k, v in os.environ.items() if k != "PR_REVIEW_NO_POST"})
    return out.returncode == 0, (out.stdout + out.stderr).strip()


def start_review(key):
    org, repo, n = A.parse_pr(key)
    if A.is_running(org, repo, n):
        return False, "already being reviewed"
    with open(A.LOGS / "dashboard.log", "a") as out:
        subprocess.Popen([*A.OS.LOW, sys.executable, str(BASE / "agent.py"), "review", key], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    return True, "review started"


def activity():
    f = A.LOGS / "agent.log"
    lines = f.read_text().splitlines()[-40:] if f.exists() else []
    for org, label in LABELS.items():
        lines = [l.replace(f"{org}/", f"{label} ") for l in lines]
    return list(reversed(lines))


class Handler(BaseHTTPRequestHandler):
    server_version = "review-desk"

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "font-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, api):
        # Host check stops DNS rebinding, the token stops other local pages and sites
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        if api:
            return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)
        t = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return secrets.compare_digest(t, TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        api = u.path.startswith("/api/")
        if not self.allowed(api):
            return self.send(403, b"Open the dashboard with: prr dashboard", "text/plain; charset=utf-8")
        q = parse_qs(u.query)
        if u.path == "/":
            return self.send(200, (BASE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self.send(200, build_state())
        if u.path == "/api/draft":
            try:
                return self.send(200, draft_view(q.get("key", [""])[0]))
            except SystemExit as exc:
                return self.send(400, {"error": str(exc)})
        if u.path == "/api/frame":
            return self.send(200, FRAME.frame(BASE.parent, BASE.parent / "agent-lib", "pr-review-agent"))
        if u.path == "/api/activity":
            return self.send(200, {"lines": activity()})
        if u.path == "/api/run-settings":
            return self.send(200, run_settings_view())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self.allowed(True) or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(403, {"error": "forbidden"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        key = body.get("key", "")
        try:
            if u.path == "/api/review":
                ok, msg = start_review(key)
            elif u.path == "/api/post":
                ok, msg = run_prr("post", key)
            elif u.path == "/api/skip":
                ok, msg = run_prr("skip", key)
            elif u.path == "/api/agent":
                action = body.get("action")
                if action == "pause":
                    ok, msg = run_prr("pause")
                elif action == "resume":
                    ok, msg = run_prr("resume")
                elif action == "check":
                    ok, err = A.OS.service_kick(A.SCHEDULER, A.LABEL, [sys.executable, str(BASE / "agent.py"), "cycle"])
                    threading.Thread(target=fetch_requests, daemon=True).start()
                    msg = err or "check started"
                else:
                    ok, msg = False, "unknown action"
            elif u.path == "/api/run-settings":
                data = run_settings_data()
                m, e = A.RS.clean(body.get("model"), body.get("effort"), data)
                data["defaults"]["review"] = {"model": m, "effort": e}
                A.RS.save(A.RUN_SETTINGS, data)
                ok, msg = True, f"Reviews now run on {A.RS.describe(m, e, data)}."
            elif u.path == "/api/job-settings":
                A.parse_pr(key)
                entry = A.set_override(key, body.get("model"), body.get("effort"))
                ok, msg = True, (f"This PR now runs on {A.RS.describe(entry.get('model'), entry.get('effort'), run_settings_data())}."
                                 if entry else "This PR uses the default again.")
            elif u.path == "/api/models/check":
                if not probe_state["running"]:
                    threading.Thread(target=probe_models, daemon=True).start()
                ok, msg = True, "Checking which models work, it takes about a minute."
            elif u.path == "/api/refresh":
                fetch_requests()
                ok, msg = cache["error"] is None, cache["error"] or "refreshed"
            else:
                return self.send(404, {"error": "not found"})
        except SystemExit as exc:
            ok, msg = False, str(exc)
        except Exception as exc:
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        self.send(200 if ok else 400, {"ok": ok, "message": msg})


def main():
    port = A.CFG.get("dashboard_port", 8787)
    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError:
        server = LocalServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    A.DASH.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": TOKEN, "started": A.now()}))
    A.DASH.chmod(0o600)
    atexit.register(lambda: A.DASH.unlink(missing_ok=True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=refresher, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
