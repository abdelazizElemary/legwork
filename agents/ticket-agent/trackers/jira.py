"""Jira as the ticket source, read-only.

auth "pat":     Data Center personal access token, sent as a Bearer token (env var named in token_env)
auth "basic":   Jira Cloud, email plus API token (env var named in token_env)
auth "browser": SSO sites with no tokens. Sign-in happens once in a real Chrome window (Playwright, in the
                agent's venv). The session cookies are saved and every API call is a plain HTTPS GET with
                them. When the session lapses, a headless Chrome retries sso_url quietly, and only when the
                identity provider wants the user again does the dashboard ask for a sign-in.

python3 trackers/jira.py whoami | login | refresh      (login and refresh need the venv for Playwright)
"""

import base64
import fcntl
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trackers import SignInNeeded  # noqa: E402


class JiraTracker:
    name = "Jira"
    format = "jira"

    def __init__(self, cfg, base):
        self.cfg = cfg
        self.base = Path(base)
        self.url = cfg["base_url"].rstrip("/")
        self.auth = cfg.get("auth", "pat")
        self.can_login = self.auth == "browser"
        self.cookies = self.base / ".jira_cookies.json"
        self.profile = self.base / "browser-profile"
        self.browser_lock = self.base / ".browser.lock"
        self.sso = cfg.get("sso_url") or f"{self.url}/login.jsp"
        self.api = cfg.get("api_version", "2")
        self.search_path = cfg.get("search_path") or f"/rest/api/{self.api}/search"

    # ---------- HTTP ----------

    def _headers(self, raw):
        h = {"Accept": "*/*" if raw else "application/json", "User-Agent": "ticket-agent"}
        token = os.environ.get(self.cfg.get("token_env") or "JIRA_TOKEN", "")
        if not token and self.cfg.get("token_file"):
            token = Path(os.path.expanduser(self.cfg["token_file"])).read_text().strip()
        if self.auth == "pat" and token:
            h["Authorization"] = f"Bearer {token}"
        elif self.auth == "basic" and token:
            h["Authorization"] = "Basic " + base64.b64encode(f"{self.cfg.get('email', '')}:{token}".encode()).decode()
        return h

    def _jar(self):
        jar = http.cookiejar.CookieJar()
        for c in json.loads(self.cookies.read_text()) if self.cookies.exists() else []:
            domain = c["domain"]
            jar.set_cookie(http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False, domain, True, domain.startswith("."), c.get("path", "/"), True,
                c.get("secure", False), int(c["expires"]) if c.get("expires", -1) > 0 else None, False, None, None, {}))
        return jar

    def _save_jar(self, jar):
        old = {(c["name"], c["domain"], c.get("path", "/")): c for c in json.loads(self.cookies.read_text())} \
            if self.cookies.exists() else {}
        for c in jar:
            old[(c.name, c.domain, c.path)] = {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                                              "secure": c.secure, "expires": c.expires or -1}
        self._write_cookies(list(old.values()))

    def _write_cookies(self, cookies):
        tmp = self.cookies.with_suffix(".tmp")
        tmp.write_text(json.dumps(cookies))
        tmp.chmod(0o600)
        tmp.replace(self.cookies)

    def get(self, path, raw=False, retry=True):
        """GET a Jira path. Raises SignInNeeded when the credentials or the session don't work."""
        handlers = [_NoRedirect]
        jar = None
        if self.auth == "browser":
            jar = self._jar()
            handlers.append(urllib.request.HTTPCookieProcessor(jar))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(self.url + path, headers=self._headers(raw))
        try:
            with opener.open(req, timeout=60) as r:
                body = r.read()
            if jar is not None:
                self._save_jar(jar)
            return body if raw else json.loads(body)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 401, 403):
                if retry and self.auth == "browser" and self.refresh():
                    return self.get(path, raw, retry=False)
                raise SignInNeeded("Jira session expired, sign in again" if self.auth == "browser"
                                   else f"Jira refused the token ({exc.code}), check {self.cfg.get('token_env') or 'JIRA_TOKEN'}") from None
            raise

    # ---------- the interface ----------

    def whoami(self):
        try:
            me = self.get(f"/rest/api/{self.api}/myself", retry=False)
            return {"ok": True, "name": me.get("displayName"), "user": me.get("name") or me.get("accountId")}
        except SignInNeeded as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def search(self):
        fields = "summary,status,issuetype,priority,updated,created"
        q = urllib.parse.quote(self.cfg["jql"])
        r = self.get(f"{self.search_path}?maxResults=100&fields={fields}&jql={q}")
        rows = []
        for i in r.get("issues", []):
            f = i["fields"]
            rows.append({"key": i["key"], "summary": f.get("summary"), "type": f["issuetype"]["name"],
                         "status": f["status"]["name"], "priority": (f.get("priority") or {}).get("name"),
                         "updated": f.get("updated"), "url": f"{self.url}/browse/{i['key']}",
                         "source": {"key": i["key"]}})
        return rows

    def fetch(self, st, folder):
        key = st["key"]
        issue = self.get(f"/rest/api/{self.api}/issue/{key}?fields=*all")
        f = issue["fields"]
        folder = Path(folder)
        att = folder / "attachments"
        att.mkdir(parents=True, exist_ok=True)
        for a in f.get("attachment") or []:
            target = att / Path(a["filename"]).name
            if target.exists() or a["size"] > 15_000_000:
                continue
            try:
                target.write_bytes(self.get(urllib.parse.urlparse(a["content"]).path, raw=True))
            except Exception:
                continue
        epic_field = self.cfg.get("epic_field")
        links = [{"type": l["type"].get("outward") if l.get("outwardIssue") else l["type"].get("inward"),
                  "key": (l.get("outwardIssue") or l.get("inwardIssue") or {}).get("key")}
                 for l in f.get("issuelinks") or []]
        for l in links:
            l["url"] = f"{self.url}/browse/{l['key']}"
        epic = f.get(epic_field) if epic_field else None
        view = {
            "format": "jira", "url": f"{self.url}/browse/{key}", "description": f.get("description") or "",
            "reporter": (f.get("reporter") or {}).get("displayName"), "created": f.get("created"),
            "comments": [{"author": (c.get("author") or {}).get("displayName"), "created": c.get("created"),
                          "body": c.get("body") or ""} for c in (f.get("comment") or {}).get("comments", [])],
            "links": links + ([{"type": "epic", "key": epic, "url": f"{self.url}/browse/{epic}"}] if epic else []),
            "parent": (f.get("parent") or {}).get("key"),
        }
        (folder / "raw.json").write_text(json.dumps(issue, indent=1))
        (folder / "issue.json").write_text(json.dumps(view, indent=1))
        (folder / "ticket.md").write_text(self._render(issue, view))
        return {"summary": f.get("summary"), "updated": f.get("updated")}

    def _render(self, issue, view):
        f = issue["fields"]
        head = [f"# {issue['key']}: {f.get('summary')}", "",
                f"Type: {f['issuetype']['name']}. Status: {f['status']['name']}. "
                f"Priority: {(f.get('priority') or {}).get('name')}. Reporter: {view['reporter']}.",
                f"Jira: {view['url']}"]
        if view.get("parent"):
            head.append(f"Parent: {view['parent']}")
        if view["links"]:
            head.append("Links: " + "; ".join(f"{l['type']} {l['key']}" for l in view["links"]))
        for s in f.get("subtasks") or []:
            head.append(f"Subtask: {s['key']} {s['fields']['summary']} ({s['fields']['status']['name']})")
        parts = head + ["", "## Description", "", view["description"] or "(empty)", "", "## Comments", ""]
        for c in view["comments"]:
            parts += [f"### {c['author']}, {(c['created'] or '')[:16].replace('T', ' ')}", "", c["body"], ""]
        if not view["comments"]:
            parts.append("(none)")
        parts += ["", "## Attachments", ""]
        for a in f.get("attachment") or []:
            parts.append(f"- {a['filename']} ({a['mimeType']}, {a['size']} bytes) -> attachments/{a['filename']}")
        return "\n".join(parts) + "\n"

    def mentions(self, st):
        return [st["key"]]

    def issue_line(self, st):
        fmt = self.cfg.get("issue_line", "")
        return fmt.format(ref=st["key"], key=st["key"], url=st.get("url", "")) if fmt else ""

    # ---------- browser sign-in ----------

    def _browser(self, headless):
        from playwright.sync_api import sync_playwright
        lock = open(self.browser_lock, "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(str(self.profile), channel="chrome", headless=headless,
                                                    viewport={"width": 1100, "height": 800})
        if self.cookies.exists():
            try:
                ctx.add_cookies([{k: v for k, v in c.items() if k in ("name", "value", "domain", "path", "secure", "expires")}
                                 for c in json.loads(self.cookies.read_text())])
            except Exception:
                pass
        return pw, ctx, lock

    def _signed_in(self, ctx):
        r = ctx.request.get(f"{self.url}/rest/api/{self.api}/myself", headers={"Accept": "application/json"}, max_redirects=0)
        return r.status == 200

    def _keep(self, ctx):
        self._write_cookies([{k: c[k] for k in ("name", "value", "domain", "path", "secure", "expires")} for c in ctx.cookies()])

    def refresh(self):
        """Quiet headless SSO renewal. True when the session works again."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            # the agent runs on the system python, Playwright lives in the agent venv
            out = subprocess.run([str(self.base / ".venv" / "bin" / "python"), str(Path(__file__).resolve()), "refresh"],
                                 capture_output=True, text=True, timeout=120)
            try:
                return json.loads(out.stdout.strip().splitlines()[-1]).get("ok", False)
            except (ValueError, IndexError):
                return False
        try:
            pw, ctx, lock = self._browser(headless=True)
        except Exception:
            return False
        try:
            page = ctx.new_page()
            page.goto(self.sso, wait_until="domcontentloaded", timeout=45000)
            for _ in range(30):
                if page.url.startswith(self.url) and self._signed_in(ctx):
                    self._keep(ctx)
                    return True
                page.wait_for_timeout(1000)
            return False
        except Exception:
            return False
        finally:
            ctx.close(); pw.stop(); lock.close()

    def login(self, timeout_s=600):
        """Open Chrome on the sign-in page and wait until Jira answers as signed in."""
        pw, ctx, lock = self._browser(headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(self.sso, wait_until="domcontentloaded", timeout=60000)
            end = time.time() + timeout_s
            while time.time() < end:
                if page.url.startswith(self.url) and self._signed_in(ctx):
                    self._keep(ctx)
                    page.wait_for_timeout(1500)
                    return {"ok": True}
                page.wait_for_timeout(2000)
            return {"ok": False, "error": "sign-in timed out"}
        finally:
            ctx.close(); pw.stop(); lock.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent
    t = JiraTracker(json.loads((base / "config.json").read_text())["tracker"], base)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "whoami"
    if cmd == "login":
        print(json.dumps(t.login()))
    elif cmd == "refresh":
        print(json.dumps({"ok": t.refresh()}))
    else:
        print(json.dumps(t.whoami()))
