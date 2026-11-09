"""Issue trackers the ticket agent reads from. Every tracker is read-only and has the same shape:

    name, can_login, format ("markdown" or "jira")
    whoami()          -> {"ok", "name", "error"}
    search()          -> [{"key", "summary", "type", "status", "priority", "updated", "url", "source"}]
                         the open tickets assigned to you
    fetch(st, folder) -> {"summary", "updated"}, and writes ticket.md, issue.json (the dashboard view),
                         raw.json and attachments/ into folder
    mentions(st)      -> strings a commit message may use to name the ticket
    issue_line(st)    -> the line the agent appends to each PR body ("" for none)
    login()           -> only when can_login

Keys are always PREFIX-NUMBER (e.g. WEB-42), so they are safe in file names and branch names.
"""

import re


class SignInNeeded(Exception):
    pass


KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def load(cfg, base):
    kind = cfg.get("type")
    if kind == "github":
        from trackers.github import GitHubTracker
        return GitHubTracker(cfg, base)
    if kind == "jira":
        from trackers.jira import JiraTracker
        return JiraTracker(cfg, base)
    raise SystemExit(f"unknown tracker type {kind!r}, use github or jira")
