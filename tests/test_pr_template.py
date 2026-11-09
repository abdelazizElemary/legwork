"""When a repo has a PR template, the ticket agent's PRs follow it: every section kept and filled, checked before
anything is pushed. The person's style switches then judge the wording, not the template's structure."""

import json

from conftest import config, git, install, load_module
from test_post_review import ALL_STYLE
from test_ticket_push import commit, remote_head, ticket, worktree

TEMPLATE = """## What changed

<!-- what and why -->

## Related issue

Fixes #issue-number

## Type of change

- [ ] Bug fix
- [ ] New feature

## How it was tested

- [ ] Tests added or updated

## Notes (optional)

<!-- anything reviewers should know -->
"""

FILLED = """## What changed

The session now stays alive when the network drops. **Users** no longer lose their work.

## Related issue

Fixes #9

## Type of change

- [x] Bug fix
- [ ] New feature

## How it was tested

- [x] Tests added or updated

`pytest tests/test_session.py` passes, and fails on main.

## Notes (optional)

<!-- anything reviewers should know -->
"""


def with_template(origin):
    seed = origin["seed"]
    (seed / ".github").mkdir()
    (seed / ".github/pull_request_template.md").write_text(TEMPLATE)
    git("add", ".", cwd=seed)
    git("commit", "-qm", "add a pr template", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    git("fetch", "-q", "origin", cwd=origin["clone"])


def body_sent(fakes):
    calls = [json.loads(l) for l in fakes["log"].read_text().splitlines() if l.startswith('["pr", "create"')]
    return open(calls[0][calls[0].index("--body-file") + 1]).read()


def test_a_plain_body_is_refused_when_the_repo_has_a_template(ticket_agent, origin, home, tmp_path):
    with_template(origin)
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(ticket_agent, tmp_path, wt)
    st = ticket_agent.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("template sections are missing" in e for e in st["failure"]["errors"])
    assert remote_head(origin) is None


def test_an_empty_section_or_placeholder_is_refused(ticket_agent, origin, home, tmp_path):
    with_template(origin)
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    m = ticket(ticket_agent, tmp_path, wt)
    data = json.loads(m.read_text())
    data["prs"][0]["body"] = TEMPLATE.replace("<!-- what and why -->", "Keeps the session alive.")
    m.write_text(json.dumps(data))
    errs = ticket_agent.finalize("DEMO-9")["failure"]["errors"]
    assert any("'type of change' section is empty" in e for e in errs) and any("placeholder is left in (#issue-number)" in e for e in errs)


def test_a_filled_template_is_pushed_without_a_second_ticket_line(ticket_agent, origin, home, tmp_path, fakes):
    with_template(origin)
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    m = ticket(ticket_agent, tmp_path, wt)
    data = json.loads(m.read_text())
    data["prs"][0]["body"] = FILLED
    m.write_text(json.dumps(data))
    st = ticket_agent.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("error")
    sent = body_sent(fakes)
    assert "## What changed" in sent and "Refs acme/demo#9" not in sent, "the template's related issue already links it"


def test_conventions_point_the_run_at_the_template(ticket_agent, origin):
    with_template(origin)
    st = {"key": "DEMO-9", "source": {"repo": "acme/demo", "number": 9, "ref": "acme/demo#9"}}
    d = ticket_agent.TICKETS / "DEMO-9"
    d.mkdir(parents=True, exist_ok=True)
    ticket_agent.write_conventions(d, st)
    text = (d / "conventions.md").read_text()
    assert ".github/pull_request_template.md" in text and "Keep every section" in text


def test_without_a_template_the_ticket_line_is_still_added(ticket_agent, origin, home, tmp_path, fakes):
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(ticket_agent, tmp_path, wt)
    assert ticket_agent.finalize("DEMO-9")["phase"] == "pr_open"
    assert "Refs acme/demo#9" in body_sent(fakes)


def test_template_structure_passes_a_strict_style_but_the_wording_is_still_checked(tmp_path, fakes):
    home = install(tmp_path, config(tmp_path, fakes, style=ALL_STYLE))
    m = load_module(home / ".claude/skills/pr-review/scripts/post_review.py", "post_review_template")
    e, w = [], []
    m.lint_text("body", FILLED, e, w, inline=False, structure=False)
    assert not e, e
    for bad, needle in (("Generated with Claude Code", "attribution"), ("Fixed 🚀", "emoji")):
        e, w = [], []
        m.lint_text("body", FILLED + bad, e, w, inline=False, structure=False)
        assert any(needle in x for x in e), needle
    e, w = [], []
    m.lint_text("body", FILLED, e, w, inline=False)
    assert any("header" in x for x in e), "a body that isn't a template still gets the full style"
