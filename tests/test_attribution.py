"""attribution.mode: "none" (the default) keeps every tool mention out of what goes out. "disclose" has the kit's own
code add the disclosure, a trailer from the git guard and a footer on what gets posted, so it never depends on the model."""

import json
import os
import subprocess
import sys

import pytest

from conftest import KIT, config, forget, install, load_module, write_exe
from test_replies import state_with
from test_ticket_push import commit, remote_head, ticket, worktree

TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
FOOTER = "Drafted with Claude Code."
RECORDER = '#!/bin/sh\necho "$@" >> "$SHIM_LOG"\n'


def installed(tmp_path, fakes, attribution=None, **binaries):
    cfg = config(tmp_path, fakes)
    if attribution is not None:
        cfg["attribution"] = attribution
    cfg["binaries"].update(binaries)
    return install(tmp_path, cfg)


def agent(home, fakes, monkeypatch, name):
    monkeypatch.setenv("FAKE_GH_LOG", str(fakes["log"]))
    A = load_module(home / ".claude/ticket-agent/agent.py", name)
    A.spawn = lambda *a: None
    A.notify = lambda *a: None
    A.CFG["diagnose"] = False
    return A


@pytest.fixture(autouse=True)
def forget_installs(tmp_path):
    yield
    forget(tmp_path)


@pytest.fixture
def disclosing(tmp_path, fakes, monkeypatch):
    home = installed(tmp_path, fakes, {"mode": "disclose"})
    return agent(home, fakes, monkeypatch, "ticket_agent_disclosing"), home


# ---------- install ----------

def test_none_is_the_default_and_keeps_the_rules_as_they_were(home):
    guard = json.loads((home / ".claude/agent-lib/guard.json").read_text())
    policy = json.loads((home / ".claude/skills/pr-review/references/policy.json").read_text())
    assert "commit_trailer" not in guard and policy["attribution"]["mode"] == "none"
    assert "Never add a `Co-Authored-By` trailer" in (home / ".claude/CLAUDE.md").read_text()
    for skill in ("pr-review/SKILL.md", "ticket-work/SKILL.md"):
        text = (home / ".claude/skills" / skill).read_text()
        assert "Never mention Claude, AI" in text and "is the only author." in text


def test_disclose_reaches_the_guard_the_policy_and_the_rules(tmp_path, fakes):
    home = installed(tmp_path, fakes, {"mode": "disclose"})
    guard = json.loads((home / ".claude/agent-lib/guard.json").read_text())
    policy = json.loads((home / ".claude/skills/pr-review/references/policy.json").read_text())
    assert guard["commit_trailer"] == TRAILER
    assert policy["attribution"] == {"mode": "disclose", "commit_trailer": TRAILER, "footer": FOOTER}
    rules = (home / ".claude/CLAUDE.md").read_text()
    assert "I disclose AI help" in rules and TRAILER in rules and "Never add a `Co-Authored-By`" not in rules
    for skill in ("pr-review/SKILL.md", "ticket-work/SKILL.md"):
        text = (home / ".claude/skills" / skill).read_text()
        assert "the kit adds" in text and "Never mention Claude, AI" not in text
    assert FOOTER in (home / ".claude/skills/pr-review/references/policy.md").read_text()


@pytest.mark.parametrize("bad", [{"mode": "sometimes"}, {"mode": "disclose", "footer": ""},
                                 {"mode": "disclose", "commit_trailer": "no colon here"}])
def test_a_bad_attribution_setting_stops_the_install(tmp_path, fakes, bad):
    cfg = config(tmp_path, fakes, attribution=bad)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    out = subprocess.run([sys.executable, str(KIT / "install.py"), "--config", str(path), "--home", str(tmp_path / "h")],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "attribution" in out.stdout + out.stderr


# ---------- the git guard ----------

@pytest.mark.parametrize("attribution,trailer", [(None, False), ({"mode": "disclose"}, True)])
def test_the_guard_adds_the_trailer_only_when_disclosing(tmp_path, fakes, attribution, trailer):
    real = tmp_path / "real"
    real.mkdir()
    home = installed(tmp_path, fakes, attribution, git=str(write_exe(real / "git", RECORDER)))
    wt = tmp_path / "ws" / ".local" / "wt" / "demo-x"
    wt.mkdir(parents=True)
    log = tmp_path / "shim.log"
    out = subprocess.run([sys.executable, str(home / ".claude/agent-lib/shims/git"), "commit", "-qm", "fix: x (#9)"],
                         cwd=wt, capture_output=True, text=True, env={**os.environ, "SHIM_LOG": str(log)})
    assert out.returncode == 0, out.stderr
    assert (f"--trailer {TRAILER}" in log.read_text()) is trailer, log.read_text()


# ---------- reviews ----------

def test_disclose_adds_the_footer_to_the_review_once(tmp_path, fakes):
    home = installed(tmp_path, fakes, {"mode": "disclose"})
    m = load_module(home / ".claude/skills/pr-review/scripts/post_review.py", "post_review_disclosing")
    errors, warnings = [], []
    m.lint_text("body", "Generated with Claude Code", errors, warnings, inline=False)
    assert not errors, "a disclosure is allowed when disclosing"
    assert m.review_payload("abc", "APPROVE", "", [])["body"] == FOOTER, "a silent approve carries only the footer"
    assert m.review_payload("abc", "COMMENT", f"one question\n\n{FOOTER}", [])["body"].count(FOOTER) == 1


def test_none_posts_the_body_as_written(neutral_review):
    assert neutral_review.review_payload("abc", "APPROVE", "", [])["body"] == ""
    errors, warnings = [], []
    neutral_review.lint_text("body", "Generated with Claude Code", errors, warnings, inline=False)
    assert any("attribution" in e for e in errors)


@pytest.fixture
def neutral_review(home):
    return load_module(home / ".claude/skills/pr-review/scripts/post_review.py", "post_review_none")


# ---------- ticket PRs, commits and replies ----------

def test_disclose_needs_the_trailer_on_every_commit(disclosing, origin, tmp_path):
    A, home = disclosing
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt)
    st = A.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("trailer" in e for e in st["failure"]["errors"])
    assert remote_head(origin) is None


def test_disclose_pushes_a_trailed_commit_and_ends_the_pr_body_with_the_footer(disclosing, origin, tmp_path):
    A, home = disclosing
    wt = worktree(home, origin)
    commit(wt, "b.txt", f"fix: keep the session alive (#9)\n\n{TRAILER}")
    ticket(A, tmp_path, wt)
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("error")
    body = (A.RUNS / "DEMO-9" / "r1" / "demo-body.md").read_text()
    assert body.rstrip().endswith(FOOTER) and body.count(FOOTER) == 1


def test_none_refuses_a_trailed_commit(ticket_agent, origin, home, tmp_path):
    wt = worktree(home, origin)
    commit(wt, "b.txt", f"fix: keep the session alive (#9)\n\n{TRAILER}")
    ticket(ticket_agent, tmp_path, wt)
    st = ticket_agent.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("attribution" in e for e in st["failure"]["errors"])
    assert remote_head(origin) is None


def test_disclose_ends_each_reply_with_the_footer(disclosing, fakes, monkeypatch, tmp_path):
    A, _ = disclosing
    st = state_with(A, fakes, monkeypatch, tmp_path)
    fakes["log"].write_text("")
    posted, failed = A.post_replies(st, {"replies": [{"repo": "demo", "pr": 7, "to": "comment-4242", "body": "fixed in b.py"}]})
    body = [json.loads(l) for l in fakes["log"].read_text().splitlines()][-1][-1]
    assert posted and not failed and body.rstrip().endswith(FOOTER)


@pytest.mark.parametrize("mode", ["none", "disclose"])
def test_the_run_is_told_who_writes_the_attribution(tmp_path, fakes, monkeypatch, mode):
    home = installed(tmp_path, fakes, {"mode": mode})
    A = agent(home, fakes, monkeypatch, f"ticket_agent_{mode}")
    A.write_conventions(tmp_path, {"key": "DEMO-9", "source": {"repo": "acme/demo", "number": 9, "ref": "acme/demo#9"}})
    text = (tmp_path / "conventions.md").read_text()
    assert ("No trailers, no attribution." in text) is (mode == "none")
    assert (f"The git guard adds `{TRAILER}`" in text) is (mode == "disclose")
