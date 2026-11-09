#!/usr/bin/env python3
"""Install legwork for one user. A dry run by default: it prints every file it would write.

python3 install.py --config kit.config.json                  show the plan, write nothing
python3 install.py --config kit.config.json --apply          write the files
python3 install.py --config kit.config.json --apply --load   also start the scheduler jobs
  --home DIR   install under another home folder (for a test run)
  --force      replace code files that differ from the kit (a .bak copy is kept). Config and state are never replaced.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent
BEGIN, END = "<!-- agent-kit:begin -->", "<!-- agent-kit:end -->"
TOOLS = ("node", "npm", "npx", "uv", "uvx", "jq", "pnpm", "yarn", "go", "java", "mvn", "docker", "podman")


def die(msg):
    sys.exit(f"error: {msg}")


def which(name):
    # the PATH entry as found, not its symlink target: a versioned target breaks when the tool upgrades
    return shutil.which(name)


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    a = cfg.get("author") or {}
    if not a.get("name") or not a.get("email") or "@" not in a["email"]:
        die("author.name and author.email are required")
    if not cfg.get("projects"):
        die("projects needs at least one {id, org, label, workspace}")
    for p in cfg["projects"]:
        for k in ("id", "org", "label", "workspace"):
            if not p.get(k):
                die(f"project {p} is missing {k}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", p["id"]):
            die(f"project id {p['id']!r} must be lowercase kebab-case")
    comps = cfg.setdefault("components", ["pr-review", "ticket"])
    if "ticket" in comps:
        t = cfg.get("ticket") or die("components has ticket but there's no ticket section")
        if t.get("project") not in {p["id"] for p in cfg["projects"]}:
            die("ticket.project must be one of the project ids")
        tr = t.get("tracker") or die("ticket.tracker is required")
        if tr.get("type") == "jira" and not (tr.get("base_url") and tr.get("jql")):
            die("a jira tracker needs base_url and jql")
        if tr.get("type") not in ("github", "jira"):
            die("ticket.tracker.type must be github or jira")
        if t.get("rework_branch", "same") not in ("same", "new"):
            die("ticket.rework_branch must be same or new")
        if not isinstance(t.get("active_statuses", []), list) or not all(isinstance(x, str) for x in t.get("active_statuses", [])):
            die("ticket.active_statuses must be a list of status names")
    attribution(cfg)
    return cfg


def resolve_env(cfg, home):
    b = cfg.setdefault("binaries", {})
    for name, exe in (("python", "python3"), ("claude", "claude"), ("gh", "gh"), ("git", "git")):
        b[name] = b.get(name) or which(exe) or ""
        if not b[name]:
            die(f"can't find {exe} on PATH, set binaries.{name} in the config")
    v = subprocess.run([b["python"], "-c", "import sys; print(sys.version_info >= (3, 9))"], capture_output=True, text=True)
    if v.stdout.strip() != "True":
        die(f"{b['python']} must be Python 3.9 or newer")
    gv = re.search(r"(\d+)\.(\d+)", subprocess.run([b["git"], "--version"], capture_output=True, text=True).stdout)
    if attribution(cfg)["mode"] == "disclose" and gv and tuple(map(int, gv.groups())) < (2, 32):
        die(f"attribution.mode disclose needs git 2.32 or newer for commit trailers, {b['git']} is {gv.group(0)}")
    if not cfg.get("run_path"):
        dirs = [os.path.dirname(b[k]) for k in ("python", "claude", "gh", "git")]
        dirs += [os.path.dirname(which(t)) for t in TOOLS if which(t)]
        dirs += ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        cfg["run_path"] = ":".join(dict.fromkeys(d for d in dirs if d))
    if not cfg.get("scheduler"):
        cfg["scheduler"] = "launchd" if sys.platform == "darwin" else ("systemd" if which("systemctl") else "cron")
    cfg["service_prefix"] = cfg.get("service_prefix") or f"com.{re.sub(r'[^a-z0-9]', '', getpass.getuser().lower()) or 'me'}"
    cfg["github_host"] = cfg.get("github_host") or "github.com"
    cfg["interval_minutes"] = cfg.get("interval_minutes") or 10
    if cfg.get("shell_env") is None:
        cfg["shell_env"] = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"]
    return cfg


# a person's own voice, habits and review style live in profile/ (not in git) and their config
STYLE_DEFAULTS = {"no_emoji": False, "no_headers": False, "no_bold": False, "no_tables": False, "no_bullets": False,
                  "no_em_dash": False, "no_thanks_opener": False, "no_filler": False, "no_severity_labels": False,
                  "max_words_inline": 0, "max_words_body": 0}
# "none" keeps every tool mention out; "disclose" has the kit's own code add a commit trailer and a footer
ATTRIBUTION_DEFAULTS = {"mode": "none", "commit_trailer": "Co-Authored-By: Claude <noreply@anthropic.com>",
                        "footer": "Drafted with Claude Code."}
STYLE_TEXT = {"no_emoji": "no emoji", "no_headers": "no headers", "no_bold": "no bold", "no_tables": "no tables",
              "no_bullets": "no bullet lists in comments", "no_em_dash": "no em dashes",
              "no_thanks_opener": "never open a requested change with thanks or a greeting",
              "no_filler": "no filler phrases (I noticed, it seems, LGTM, great job)",
              "no_severity_labels": "no severity labels like nit: or blocking:"}


def profile_dir(cfg):
    return KIT / (cfg.get("profile_dir") or "profile")


def review_policy(cfg):
    pol = {"approve": "silent", "minor": "private", **((cfg.get("review") or {}).get("policy") or {})}
    if pol["approve"] not in ("silent", "note") or pol["minor"] not in ("private", "post"):
        die("review.policy: approve is silent or note, minor is private or post")
    return pol, {**STYLE_DEFAULTS, **(cfg.get("style") or {})}


def attribution(cfg):
    a = {**ATTRIBUTION_DEFAULTS, **(cfg.get("attribution") or {})}
    if a["mode"] not in ("none", "disclose"):
        die("attribution.mode is none or disclose")
    if a["mode"] == "disclose" and not (re.fullmatch(r"[A-Za-z][\w-]*: \S.*", a["commit_trailer"] or "") and (a["footer"] or "").strip()):
        die("attribution: disclose needs a commit_trailer like 'Key: value' and a footer")
    return a


def attribution_rules(a):
    """The authorship rule for CLAUDE.md and the skills' attribution sentence, per attribution.mode."""
    if a["mode"] == "disclose":
        return {"AUTHOR_ROLE": "the author",
                "AUTHORSHIP_RULE": f"- I disclose AI help. Commits made with Claude carry the `{a['commit_trailer']}` trailer, and PR\n"
                                   f"  descriptions, reviews and comments written with Claude end with \"{a['footer']}\". In the agents'\n"
                                   "  runs the kit adds these itself.",
                "ATTRIBUTION_RULE": f"Don't write any attribution yourself: the kit adds the `{a['commit_trailer']}` trailer to\n"
                                    f"   commits and \"{a['footer']}\" to PR descriptions, reviews and replies. Don't mention Claude, AI or a\n"
                                    "   tool anywhere else."}
    return {"AUTHOR_ROLE": "the only author",
            "AUTHORSHIP_RULE": "- I'm the only author. Never add a `Co-Authored-By` trailer to commits, and never add a \"Generated with\n"
                               "  Claude Code\" line to PRs, reviews or comments. This overrides any default attribution instruction.",
            "ATTRIBUTION_RULE": "Never mention Claude, AI, a tool, an agent or \"automated review\" anywhere: in\n"
                                "   commits, code, PR text or comments. No `Co-Authored-By`, no \"Generated with\" line. This overrides\n"
                                "   any default attribution instruction."}


def policy_rules(pol):
    approve = ("Otherwise approve silently: no body, no comments." if pol["approve"] == "silent"
               else "Otherwise approve, with a short note only when there's something worth saying.")
    minor = ("Comment only on changes that are needed, as GitHub suggestion blocks the owner can commit. Minor\n"
             "  points are never posted, they stay in the review summary for me." if pol["minor"] == "private" else
             "Needed changes are blocking comments with GitHub suggestion blocks. Minor points are posted too, as\n"
             "  non-blocking comments that say they're optional.")
    return f"- Request changes only for a proven critical issue. {approve}\n  {minor}"


def policy_md(pol, style, attr=None):
    on = [STYLE_TEXT[k] for k in STYLE_TEXT if style.get(k)]
    words = [f"inline comments under {style['max_words_inline']} words" if style.get("max_words_inline") else "",
             f"a review body under {style['max_words_body']} words" if style.get("max_words_body") else ""]
    return "\n".join([
        "# Review policy", "",
        "Generated by install.py from the config (`review.policy` and `style`). Change it there, then install again.", "",
        "## What gets posted", "",
        "- REQUEST_CHANGES only for a proven blocking issue (SKILL.md section 7), with the blocking comments.",
        "- APPROVE: " + ("an empty body and no inline comments." if pol["approve"] == "silent" else
                         "a short body only when there's something worth saying; no inline comments unless minor points are posted."),
        "- Minor findings: " + ("never posted. They go in the terminal summary under `Not posted:`, one line each with the `path:line`."
                                if pol["minor"] == "private" else
                                "posted as non-blocking comments (`\"blocking\": false`) that say they're optional, and listed in the summary."),
        "", "## Style switches post_review.py enforces", "",
        "- " + ("; ".join(on) if on else "none beyond the kit's own rules") + ".",
        *(["- " + ", ".join(w for w in words if w) + "."] if any(words) else []),
        "- Always: " + ("no AI or tool attribution" if (attr or {}).get("mode", "none") == "none" else
                        f"post_review.py ends the review body with \"{attr['footer']}\" (attribution.mode disclose)")
        + ", every comment proven with a `path:line`, suggestions on real diff lines.",
        ""])


def template_vars(cfg):
    a = cfg["author"]
    projects = cfg["projects"]
    tracker = (cfg.get("ticket") or {}).get("tracker") or {}
    if tracker.get("type") == "github":
        where = tracker.get("project")
        desc = (f"the GitHub Projects board {where['owner']} #{where['number']}" if where
                else "GitHub Issues" + (f" ({' '.join(tracker.get('scope') or [])})" if tracker.get("scope") else ""))
    elif tracker.get("type") == "jira":
        desc = f"Jira ({tracker.get('base_url')})"
    else:
        desc = "the tracker"
    rule = cfg.get("commit_rule") or ""
    return {
        "AUTHOR_NAME": a["name"], "AUTHOR_FIRST": a.get("first_name") or a["name"].split()[0], "AUTHOR_EMAIL": a["email"],
        "GH_HOST": cfg["github_host"],
        "WORKSPACE_LIST": ", ".join(f"`{p['workspace']}`" for p in projects),
        "ORG_LIST": ", ".join(f"`{p['org']}`" for p in projects),
        "COMMIT_RULE": f" {rule}" if rule else "",
        "TRACKER_DESC": desc,
        "CHAT_REVIEW": (f"`{cfg['chat_review_skill']}` (my own review skill for these repos)" if cfg.get("chat_review_skill")
                        else "the `pr-review` skill too"),
        "REVIEW_POLICY_RULES": policy_rules(review_policy(cfg)[0]),
        **attribution_rules(attribution(cfg)),
    }


def render(text, values):
    out = re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)
    left = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if left:
        die(f"unfilled placeholders: {sorted(set(left))}")
    return out


class Plan:
    def __init__(self, force):
        self.items, self.force = [], force

    def file(self, target, content, mode=0o644, kind="code"):
        """kind: code (kit-owned, refreshed with --force), seed (written once, then the user's), block (CLAUDE.md)."""
        data = content.encode() if isinstance(content, str) else content
        self.items.append(("file", Path(target), data, mode, kind))

    def link(self, target, source):
        self.items.append(("link", Path(target), Path(source), None, "link"))

    def status(self, item):
        what, target, data, _, kind = item
        if what == "link":
            if target.is_symlink() and os.path.realpath(target) == os.path.realpath(data):
                return "same"
            if target.exists() or target.is_symlink():
                return "replace" if self.force else "differs"
            return "create"
        if not target.exists():
            return "create"
        if target.read_bytes() == data:
            return "same"
        if kind == "seed":
            return "keep"
        if kind == "block":
            return "update"
        return "replace" if self.force else "differs"

    def show(self):
        for item in self.items:
            print(f"  {self.status(item):<8} {item[1]}")

    def apply(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for item in self.items:
            what, target, data, mode, kind = item
            st = self.status(item)
            if st in ("same", "keep", "differs"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if what == "link":
                if target.exists() or target.is_symlink():
                    target.rename(target.with_name(f"{target.name}.bak-{stamp}"))
                target.symlink_to(data)
                continue
            if target.exists() and st in ("replace", "update"):
                shutil.copy2(target, target.with_name(f"{target.name}.bak-{stamp}"))
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_bytes(data)
            tmp.chmod(mode)
            tmp.replace(target)


def with_shebang(path, python):
    text = path.read_text()
    return re.sub(r"\A#!.*\n", f"#!{python}\n", text, count=1)


def claude_md_block(existing, block):
    body = f"{BEGIN}\n{block.strip()}\n{END}\n"
    if BEGIN in existing and END in existing:
        return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", lambda m: body, existing, flags=re.S)
    return (existing.rstrip() + "\n\n" if existing.strip() else "") + body


def build(cfg, home, force):
    plan = Plan(force)
    values = template_vars(cfg)
    claude = home / ".claude"
    py = cfg["binaries"]["python"]
    comps = cfg["components"]
    lib = claude / "agent-lib"
    skills = claude / "skills"
    projects = cfg["projects"]
    services = []

    # shared library and guards
    for f in ("runsettings.py", "osglue.py", "progress.py", "frame.py"):
        plan.file(lib / f, (KIT / "agents/agent-lib" / f).read_text())
    plan.file(lib / "kit_version.json", json.dumps({"version": json.loads((KIT / "kit.json").read_text())["version"]}) + "\n")
    for f in ("gh", "git"):
        plan.file(lib / "shims" / f, with_shebang(KIT / "agents/agent-lib/shims" / f, py), 0o755)
    attr = attribution(cfg)
    plan.file(lib / "guard.json", json.dumps({"real_gh": cfg["binaries"]["gh"], "real_git": cfg["binaries"]["git"],
                                              "protected": [p["workspace"] for p in projects],
                                              **({"commit_trailer": attr["commit_trailer"]} if attr["mode"] == "disclose" else {})},
                                             indent=1) + "\n")

    # skills
    own_voice = profile_dir(cfg) / "voice.md"
    voice = own_voice if own_voice.exists() else KIT / "skills/pr-review/references/voice.md"
    plan.file(skills / "pr-review/references/voice.md", render(voice.read_text(), values))
    pol, style = review_policy(cfg)
    plan.file(skills / "pr-review/references/policy.json", json.dumps({"policy": pol, "style": style, "attribution": attr},
                                                                      indent=1) + "\n")
    severity = profile_dir(cfg) / "severity.md"
    plan.file(skills / "pr-review/references/policy.md", policy_md(pol, style, attr) +
              ("\n" + render(severity.read_text(), values).strip() + "\n" if severity.exists() else ""))
    for src in sorted((KIT / "skills/pr-review").rglob("*")):
        if src.name == "voice.md":
            continue
        if src.is_file() and src.name != "_template.md" and "__pycache__" not in src.parts and src.suffix != ".pyc":
            rel = src.relative_to(KIT / "skills")
            text = src.read_text()
            if src.suffix == ".md" and src.name != "MAPPING.md":
                text = render(text, values)
            plan.file(skills / rel, text, 0o755 if src.suffix in (".sh", ".py") else 0o644)
    template = (KIT / "skills/pr-review/references/_template.md").read_text()
    plan.file(skills / "pr-review/references/_template.md", template)
    index = ["# Projects", "", "Which reference file goes with which GitHub org. Generated by legwork's install.py.", ""]
    for p in projects:
        index.append(f"- org `{p['org']}` ({p['label']}): `references/{p['id']}.md`, workspace `{p['workspace']}`"
                     + (f", project memory `{p['memory_index']}`" if p.get("memory_index") else ""))
        # a project mapped before sits in the kit's git-ignored references/, so a reinstall starts from that map
        mapped = KIT / "references" / f"{p['id']}.md"
        ref = render(mapped.read_text(), {**values, "WORKSPACE": p["workspace"], "MEMORY_INDEX":
                     f"`{p['memory_index']}`" if p.get("memory_index") else "no project memory index on this machine yet"}) \
            if mapped.exists() else \
            template.replace("{{PROJECT_LABEL}}", p["label"]).replace("{{WORKSPACE}}", p["workspace"]) \
            .replace("{{GH_HOST}}", cfg["github_host"]).replace("{{ORG}}", p["org"])
        plan.file(skills / f"pr-review/references/{p['id']}.md", ref, kind="seed")
    plan.file(skills / "pr-review/references/projects.md", "\n".join(index) + "\n")
    if "ticket" in comps:
        for src in sorted((KIT / "skills/ticket-work").rglob("*")):
            if src.is_file() and "__pycache__" not in src.parts and src.suffix != ".pyc":
                rel = src.relative_to(KIT / "skills")
                text = render(src.read_text(), values) if src.suffix == ".md" else src.read_text()
                plan.file(skills / rel, text, 0o755 if src.suffix == ".sh" else 0o644)

    common = {"author": {"name": cfg["author"]["name"], "email": cfg["author"]["email"]},
              "path": cfg["run_path"], "claude_bin": cfg["binaries"]["claude"], "gh_bin": cfg["binaries"]["gh"],
              "git_bin": cfg["binaries"]["git"], "scheduler": cfg["scheduler"], "shell_env": cfg["shell_env"],
              "interval_minutes": cfg["interval_minutes"], "notify": cfg.get("notify", True)}

    if "pr-review" in comps:
        r = cfg.get("review") or {}
        orgs = r.get("orgs") or [p["org"] for p in projects]
        by_org = {p["org"]: p for p in projects}
        agent = claude / "pr-review-agent"
        label = f"{cfg['service_prefix']}.pr-review-agent"
        for f in ("agent.py", "dashboard.py"):
            plan.file(agent / f, with_shebang(KIT / "agents/pr-review-agent" / f, py), 0o755)
        plan.file(agent / "dashboard.html", (KIT / "agents/pr-review-agent/dashboard.html").read_text())
        plan.file(agent / "config.json", json.dumps({
            **common, "host": cfg["github_host"], "service_label": label,
            "workspaces": {o: by_org[o]["workspace"] for o in orgs}, "labels": {o: by_org[o]["label"] for o in orgs},
            "max_reviews_per_cycle": r.get("max_reviews_per_cycle", 2), "max_budget_usd": r.get("max_budget_usd", 20),
            "review_timeout_minutes": r.get("review_timeout_minutes", 60),
            "max_attempts_per_commit": r.get("max_attempts_per_commit", 2), "skip_authors": r.get("skip_authors", []),
            "dashboard_port": r.get("dashboard_port", 8787)}, indent=2) + "\n", kind="seed")
        plan.file(agent / "run_settings.json", json.dumps({"defaults": {"review": {"model": None, "effort": r.get("default_effort", "high")}},
                                                          "models": []}, indent=1) + "\n", kind="seed")
        plan.link(home / ".local/bin/prr", agent / "agent.py")
        services.append((label, agent))

    if "ticket" in comps:
        t = cfg["ticket"]
        proj = next(p for p in projects if p["id"] == t["project"])
        agent = claude / "ticket-agent"
        label = f"{cfg['service_prefix']}.ticket-agent"
        for f in ("agent.py", "dashboard.py"):
            plan.file(agent / f, with_shebang(KIT / "agents/ticket-agent" / f, py), 0o755)
        plan.file(agent / "dashboard.html", (KIT / "agents/ticket-agent/dashboard.html").read_text())
        for f in ("__init__.py", "github.py", "jira.py"):
            plan.file(agent / "trackers" / f, (KIT / "agents/ticket-agent/trackers" / f).read_text())
        tracker = {**t["tracker"]}
        if tracker["type"] == "github":
            tracker.setdefault("host", cfg["github_host"])
            tracker.setdefault("issue_line", "Refs {ref}")
        plan.file(agent / "config.json", json.dumps({
            **common, "label": t.get("label") or proj["label"], "org": proj["org"], "host": cfg["github_host"],
            "workspace": proj["workspace"], "service_label": label, "tracker": tracker,
            "repos": t.get("repos", []), "base_branches": t.get("base_branches", {}),
            "auto_check": t.get("auto_check", "all"), "skip_types": t.get("skip_types", ["Epic"]),
            "checks_per_cycle": t.get("checks_per_cycle", 2), "check_budget_usd": t.get("check_budget_usd", 4),
            "implement_budget_usd": t.get("implement_budget_usd", 40),
            "check_timeout_minutes": t.get("check_timeout_minutes", 25),
            "implement_timeout_minutes": t.get("implement_timeout_minutes", 150),
            "draft_prs": t.get("draft_prs", True), "rework_branch": t.get("rework_branch", "same"),
            "rework_merge_base": bool(t.get("rework_merge_base", True)), "active_statuses": t.get("active_statuses", []),
            "dashboard_port": t.get("dashboard_port", 8788),
            "conventions": t.get("conventions", {}),
            "max_parallel": t.get("max_parallel", {"check": 3, "implement": 2, "diagnose": 3}),
            "diagnose": t.get("diagnose", True), "diagnose_budget_usd": t.get("diagnose_budget_usd", 2),
            "diagnose_timeout_minutes": t.get("diagnose_timeout_minutes", 15)}, indent=2) + "\n", kind="seed")
        plan.file(agent / "run_settings.json", json.dumps({"defaults": {"check": {"model": None, "effort": None},
                                                                        "implement": {"model": None, "effort": None},
                                                                        "diagnose": {"model": "sonnet", "effort": "high"},
                                                                        "tell": {"model": "sonnet", "effort": "medium"}},
                                                           "models": []}, indent=1) + "\n", kind="seed")
        plan.link(home / ".local/bin/ptk", agent / "agent.py")
        services.append((label, agent))

    # global rules, inside markers so other content in CLAUDE.md is left alone
    md = claude / "CLAUDE.md"
    personal = profile_dir(cfg) / "rules.md"
    rules = render((KIT / "rules/CLAUDE.md").read_text(), {**values, "PERSONAL_RULES": render(personal.read_text(), values).strip()
                   if personal.exists() else "- Writing style: follow `~/.claude/skills/pr-review/references/voice.md`."})
    if "ticket" not in comps:
        rules = re.sub(r"\n## Tickets\n.*?(?=\n## )", "\n", rules, flags=re.S)
    plan.file(md, claude_md_block(md.read_text() if md.exists() else "", rules), kind="block")

    # scheduler jobs
    interval = int(cfg["interval_minutes"])
    for label, agent in services:
        v = {"LABEL": label, "PYTHON": py, "AGENT": str(agent / "agent.py"), "RUN_PATH": cfg["run_path"],
             "LOG": str(agent / "logs" / "scheduler.log"), "INTERVAL_S": interval * 60, "INTERVAL_MIN": interval}
        if cfg["scheduler"] == "launchd":
            plan.file(home / "Library/LaunchAgents" / f"{label}.plist", render((KIT / "service/launchd.plist").read_text(), v))
        elif cfg["scheduler"] == "systemd":
            for ext in ("service", "timer"):
                plan.file(home / ".config/systemd/user" / f"{label}.{ext}", render((KIT / f"service/systemd.{ext}").read_text(), v))
    return plan, services


def load_services(cfg, home, services):
    interval = int(cfg["interval_minutes"])
    for label, agent in services:
        (agent / "logs").mkdir(parents=True, exist_ok=True)
        if cfg["scheduler"] == "launchd":
            plist = home / "Library/LaunchAgents" / f"{label}.plist"
            dom = f"gui/{os.getuid()}"
            subprocess.run(["launchctl", "bootout", f"{dom}/{label}"], capture_output=True)
            r = subprocess.run(["launchctl", "bootstrap", dom, str(plist)], capture_output=True, text=True)
            print(f"  launchd {label}: {'loaded' if r.returncode == 0 else r.stderr.strip()}")
        elif cfg["scheduler"] == "systemd":
            subprocess.run(["systemctl", "--user", "daemon-reload"])
            r = subprocess.run(["systemctl", "--user", "enable", "--now", f"{label}.timer"], capture_output=True, text=True)
            print(f"  systemd {label}.timer: {'enabled' if r.returncode == 0 else r.stderr.strip()}")
        elif cfg["scheduler"] == "cron":
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
            if label not in cur:
                line = (f"*/{interval} * * * * PATH={cfg['run_path']} {cfg['binaries']['python']} {agent / 'agent.py'} cycle "
                        f">> {agent / 'logs' / 'scheduler.log'} 2>&1 # {label}\n")
                subprocess.run(["crontab", "-"], input=cur + line, text=True)
            print(f"  cron {label}: installed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--home")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    home = Path(os.path.expanduser(a.home or "~")).resolve()
    cfg = resolve_env(load_config(a.config), home)
    plan, services = build(cfg, home, a.force)
    print(f"install into {home} ({cfg['scheduler']} scheduler, python {cfg['binaries']['python']})")
    print(f"run PATH for the agents: {cfg['run_path']}")
    plan.show()
    if any(plan.status(i) == "differs" for i in plan.items):
        print("\nfiles marked 'differs' are left alone. Look at them, then rerun with --force to replace them (a .bak copy is kept).")
    if not a.apply:
        print("\ndry run, nothing written. Rerun with --apply to install.")
        return
    plan.apply()
    for _, agent in services:
        (agent / "logs").mkdir(parents=True, exist_ok=True)
    print("\ninstalled.")
    if a.load:
        load_services(cfg, home, services)
    t = (cfg.get("ticket") or {}).get("tracker") or {}
    steps = ["Map each project into ~/.claude/skills/pr-review/references/<id>.md (see references/MAPPING.md).",
             "prr doctor" + (" and ptk doctor" if "ticket" in cfg["components"] else "") + ", then prr models to see which models work."]
    if t.get("type") == "jira" and t.get("auth") == "browser":
        steps.append(f"Browser sign-in for Jira: {cfg['binaries']['python']} -m venv ~/.claude/ticket-agent/.venv && "
                     "~/.claude/ticket-agent/.venv/bin/pip install playwright, then ptk login (it uses your installed Chrome).")
    if t.get("type") == "github" and t.get("project"):
        steps.append("GitHub Projects needs the read:project scope: gh auth refresh -s read:project --hostname "
                     + (t.get("host") or cfg["github_host"]))
    if not a.load:
        steps.append("Rerun with --apply --load to start the scheduler jobs. The first review cycle only marks the pending requests as seen.")
    print("\nnext steps:")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")


if __name__ == "__main__":
    main()
