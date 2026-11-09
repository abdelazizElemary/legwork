"""GitHub Issues (optionally one GitHub Projects board) as the ticket source, read-only, through gh.

The tickets are the open issues assigned to you. With "project" set, only issues on that board count, and
the board's Status field is the status (items in a "done" column are left out).
"""

import json
import mimetypes
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from trackers import SignInNeeded

TYPE_LABELS = {"bug": "Bug", "enhancement": "Feature", "feature": "Feature", "epic": "Epic",
               "documentation": "Docs", "docs": "Docs", "chore": "Chore"}
PRIORITY = re.compile(r"^(?:priority[:/ -]*|p)([0-4]|low|medium|high|critical|urgent)$", re.I)
IMAGE = re.compile(r"!\[[^\]]*\]\((\S+?)(?:\s+\"[^\"]*\")?\)|<img[^>]+src=\"([^\"]+)\"", re.I)
PROJECT_QUERY = """query($owner:String!,$number:Int!,$cursor:String){%(owner)s(login:$owner){projectV2(number:$number){
items(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{
status:fieldValueByName(name:%(status)s){... on ProjectV2ItemFieldSingleSelectValue{name}}
priority:fieldValueByName(name:%(priority)s){... on ProjectV2ItemFieldSingleSelectValue{name}}
sprint:fieldValueByName(name:%(sprint)s){... on ProjectV2ItemFieldIterationValue{title startDate duration}}
content{__typename ... on Issue{number title url state updatedAt repository{nameWithOwner}
assignees(first:20){nodes{login}} labels(first:30){nodes{name}}%(itype)s}}}}}}}"""


class GitHubTracker:
    name = "GitHub"
    can_login = False
    format = "markdown"

    def __init__(self, cfg, base):
        self.cfg = cfg
        self.host = cfg.get("host") or "github.com"
        self.gh_bin = cfg.get("gh_bin") or "gh"
        self.env = {**os.environ, "GH_HOST": self.host, "PATH": cfg.get("path") or os.environ.get("PATH", "")}
        self.prefixes = {k.lower(): v.upper() for k, v in (cfg.get("key_prefixes") or {}).items()}
        self.type_labels = {**TYPE_LABELS, **{k.lower(): v for k, v in (cfg.get("type_labels") or {}).items()}}
        self._login = None
        self._issue_types = True

    # ---------- gh plumbing ----------

    def _gh(self, *args):
        out = subprocess.run([self.gh_bin, *args], capture_output=True, text=True, env=self.env, timeout=120)
        if out.returncode != 0:
            err = (out.stderr or out.stdout).strip()
            if re.search(r"auth login|not logged|401|Bad credentials|authentication", err, re.I):
                raise SignInNeeded(f"gh is signed out of {self.host}, run: gh auth login --hostname {self.host}")
            raise RuntimeError(f"gh {' '.join(args[:2])}: {err[:300]}")
        return out.stdout

    def _api(self, path):
        return json.loads(self._gh("api", path))

    def _pages(self, path, limit=10):
        rows, sep = [], "&" if "?" in path else "?"
        for page in range(1, limit + 1):
            batch = self._api(f"{path}{sep}per_page=100&page={page}")
            rows += batch
            if len(batch) < 100:
                break
        return rows

    def _graphql(self, query, **variables):
        args = ["api", "graphql", "-f", f"query={query}"]
        for k, v in variables.items():
            if v is not None:
                args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
        data = json.loads(self._gh(*args))
        if data.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "") for e in data["errors"])[:300])
        return data["data"]

    # ---------- the interface ----------

    def whoami(self):
        try:
            me = self._api("user")
            self._login = me.get("login")
            return {"ok": True, "name": me.get("name") or me.get("login"), "user": me.get("login")}
        except SignInNeeded as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def key_for(self, repo, number):
        # the configured prefix, else the repo name's last word: acme/orders-api -> API-12
        words = [w for w in re.split(r"[^A-Za-z0-9]+", repo.split("/")[-1].upper()) if w]
        prefix = self.prefixes.get(repo.lower()) or (words[-1][:10] if words else "GH")
        if not prefix[0].isalpha():
            prefix = "GH" + prefix
        return f"{prefix}-{number}"

    def _type(self, labels, issue_type=None):
        if issue_type:
            return issue_type
        for l in labels:
            if l.lower() in self.type_labels:
                return self.type_labels[l.lower()]
        return "Issue"

    @staticmethod
    def _priority(labels):
        for l in labels:
            m = PRIORITY.match(l.strip())
            if m:
                return m.group(1).upper() if m.group(1).isdigit() else m.group(1).title()
        return None

    def _row(self, repo, number, title, url, updated, labels, status, priority=None, issue_type=None, sprint=None):
        return {"key": self.key_for(repo, number), "summary": title, "type": self._type(labels, issue_type),
                "status": status, "priority": priority or self._priority(labels), "updated": updated, "url": url,
                "sprint": sprint, "source": {"repo": repo, "number": number, "ref": f"{repo}#{number}"}}

    def search(self):
        if self.cfg.get("project"):
            return self._search_project()
        q = " ".join(["is:issue", "is:open", "archived:false", "assignee:@me", *(self.cfg.get("scope") or []),
                      self.cfg.get("query") or ""]).strip()
        path = "search/issues?" + urllib.parse.urlencode({"q": q, "sort": "updated", "order": "desc", "per_page": 100})
        items = self._api(path).get("items", [])
        rows = []
        for i in items:
            if i.get("pull_request"):
                continue
            repo = "/".join(i["repository_url"].rstrip("/").split("/")[-2:])
            labels = [l["name"] for l in i.get("labels") or []]
            rows.append(self._row(repo, i["number"], i["title"], i["html_url"], i["updated_at"], labels,
                                  "Open", issue_type=(i.get("type") or {}).get("name")))
        return rows

    def _search_project(self):
        p = self.cfg["project"]
        if not self._login:
            self.whoami()
        owner = "organization" if p.get("owner_type", "organization").startswith("org") else "user"
        done = {s.lower() for s in p.get("done") or ["Done"]}
        rows, cursor = [], None
        for _ in range(20):
            query = PROJECT_QUERY % {"owner": owner, "status": json.dumps(p.get("status_field") or "Status"),
                                     "priority": json.dumps(p.get("priority_field") or "Priority"),
                                     "sprint": json.dumps(p.get("sprint_field") or "Sprint"),
                                     "itype": " issueType{name}" if self._issue_types else ""}
            try:
                data = self._graphql(query, owner=p["owner"], number=int(p["number"]), cursor=cursor)
            except RuntimeError as exc:
                # older GitHub Enterprise servers have no issue types
                if self._issue_types and "issueType" in str(exc):
                    self._issue_types = False
                    continue
                raise
            proj = ((data.get(owner) or {}).get("projectV2")) or {}
            items = proj.get("items") or {}
            for n in items.get("nodes") or []:
                c = n.get("content") or {}
                if c.get("__typename") != "Issue" or c.get("state") != "OPEN":
                    continue
                if self._login not in [a["login"] for a in (c.get("assignees") or {}).get("nodes", [])]:
                    continue
                status = (n.get("status") or {}).get("name") or "No status"
                if status.lower() in done:
                    continue
                labels = [l["name"] for l in (c.get("labels") or {}).get("nodes", [])]
                sp = n.get("sprint") or {}
                sprint = {"title": sp["title"], "start": sp.get("startDate"), "days": sp.get("duration")} if sp.get("title") else None
                rows.append(self._row(c["repository"]["nameWithOwner"], c["number"], c["title"], c["url"],
                                      c["updatedAt"], labels, status, (n.get("priority") or {}).get("name"),
                                      (c.get("issueType") or {}).get("name"), sprint))
            info = items.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info["endCursor"]
        return rows

    def fetch(self, st, folder):
        src = st["source"]
        repo, n = src["repo"], src["number"]
        issue = self._api(f"repos/{repo}/issues/{n}")
        comments = self._pages(f"repos/{repo}/issues/{n}/comments")
        try:
            timeline = self._pages(f"repos/{repo}/issues/{n}/timeline", limit=5)
        except RuntimeError:
            timeline = []
        prs, seen = [], set()
        for ev in timeline:
            si = (ev.get("source") or {}).get("issue") or {}
            if ev.get("event") != "cross-referenced" or not si.get("pull_request") or si.get("html_url") in seen:
                continue
            seen.add(si["html_url"])
            merged = (si.get("pull_request") or {}).get("merged_at")
            prs.append({"type": "pull request", "key": f"{(si.get('repository') or {}).get('full_name', '')}#{si.get('number')}",
                        "title": si.get("title"), "state": "merged" if merged else si.get("state"), "url": si["html_url"]})
        folder = Path(folder)
        att = folder / "attachments"
        att.mkdir(parents=True, exist_ok=True)
        images = self._download_images([issue.get("body") or ""] + [c.get("body") or "" for c in comments], att)
        labels = [l["name"] for l in issue.get("labels") or []]
        view = {
            "format": "markdown", "url": issue["html_url"], "description": issue.get("body") or "",
            "reporter": (issue.get("user") or {}).get("login"), "created": issue.get("created_at"),
            "labels": labels, "milestone": (issue.get("milestone") or {}).get("title"),
            "comments": [{"author": (c.get("user") or {}).get("login"), "created": c.get("created_at"),
                          "body": c.get("body") or ""} for c in comments],
            "links": prs, "images": images,
        }
        (folder / "raw.json").write_text(json.dumps({"issue": issue, "comments": comments}, indent=1))
        (folder / "issue.json").write_text(json.dumps(view, indent=1))
        (folder / "ticket.md").write_text(self._render(st["key"], repo, issue, view))
        return {"summary": issue.get("title"), "updated": issue.get("updated_at")}

    def _download_images(self, bodies, att):
        """Best effort: images in the issue text that the gh token can read. Private user-attachments may refuse it."""
        try:
            token = self._gh("auth", "token", "--hostname", self.host).strip()
        except Exception:
            token = ""
        out = {}
        for body in bodies:
            for m in IMAGE.finditer(body):
                url = m.group(1) or m.group(2)
                if url in out or not url.startswith("https://"):
                    continue
                name = f"{len(out) + 1:02d}-{Path(urllib.parse.urlparse(url).path).name or 'image'}"[:80]
                existing = [p.name for p in att.iterdir() if p.stem == Path(name).stem or p.name == name]
                if existing:
                    out[url] = existing[0]
                    continue
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "ticket-agent", "Accept": "*/*"})
                    if token and urllib.parse.urlparse(url).hostname.endswith((self.host, "githubusercontent.com")):
                        # not sent on to a redirect target, signed storage URLs refuse an extra auth header
                        req.add_unredirected_header("Authorization", f"token {token}")
                    with urllib.request.urlopen(req, timeout=60) as r:
                        ctype = r.headers.get_content_type()
                        data = r.read(15_000_001)
                    if len(data) > 15_000_000 or not ctype.startswith("image/"):
                        continue
                    if not Path(name).suffix:
                        name += mimetypes.guess_extension(ctype) or ".png"
                    (att / name).write_bytes(data)
                    out[url] = name
                except (urllib.error.URLError, OSError, ValueError):
                    continue
        return out

    @staticmethod
    def _render(key, repo, issue, view):
        head = [f"# {key}: {issue.get('title')}", "",
                f"Issue: {view['url']} ({repo}#{issue['number']}). State: {issue.get('state')}. "
                f"Opened by {view['reporter']} on {(view['created'] or '')[:10]}.",
                f"Labels: {', '.join(view['labels']) or 'none'}." + (f" Milestone: {view['milestone']}." if view["milestone"] else "")]
        for l in view["links"]:
            head.append(f"Linked PR: {l['key']} {l['title']} ({l['state']}) {l['url']}")
        parts = head + ["", "## Description", "", view["description"] or "(empty)", "", "## Comments", ""]
        for c in view["comments"]:
            parts += [f"### {c['author']}, {(c['created'] or '')[:16].replace('T', ' ')}", "", c["body"], ""]
        if not view["comments"]:
            parts.append("(none)")
        parts += ["", "## Attachments", ""]
        for url, name in view["images"].items():
            parts.append(f"- {url} -> attachments/{name}")
        if not view["images"]:
            parts.append("(none downloaded; image links in the text that aren't listed here couldn't be fetched)")
        return "\n".join(parts) + "\n"

    def mentions(self, st):
        # "(#42)" names the issue too, the way GitHub links it from the same repo
        return [st["key"], st["source"]["ref"], f"#{st['source']['number']}"]

    def issue_line(self, st):
        fmt = self.cfg.get("issue_line", "Refs {ref}")
        return fmt.format(ref=st["source"]["ref"], key=st["key"], url=st.get("url", "")) if fmt else ""
