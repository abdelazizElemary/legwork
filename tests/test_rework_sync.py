"""A rework (and a fix during one) starts from the PR branch as it is on GitHub: a conflict resolved by hand, even a
force-push, is taken as it is. The agent works in its own worktree and checks only its own commits."""

from pathlib import Path

from conftest import git
from test_ticket_push import BRANCH, commit, remote_head, ticket, worktree


def opened(A, origin, home, tmp_path):
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"
    return wt


def on_github(origin, *steps):
    """Someone works on the PR branch outside the agent and pushes. Each step is (file, message, author)."""
    seed = origin["seed"]
    git("fetch", "-q", "origin", cwd=seed)
    git("checkout", "-q", "-B", "pr", f"origin/{BRANCH}", cwd=seed)
    for name, msg, *who in steps:
        commit(str(seed), name, msg, *who)
    return seed


def rework_ready(A):
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open"}
    errs = A.prepare_rework(st)
    st.update({"rework_mode": True, "rework_last": {"instructions": "check the latest"}})
    A.save(st)
    return st, errs


def test_a_merge_of_the_base_only_checks_the_agents_own_commits(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    seed = origin["seed"]
    git("checkout", "-q", "main", cwd=seed)
    commit(str(seed), "mate.txt", "feat: a teammate's change", ("Someone Else", "else@example.com"))
    git("push", "-q", "origin", "main", cwd=seed)

    st, errs = rework_ready(A)
    assert errs == []
    git("fetch", "-q", "origin", cwd=wt)
    git("-c", "user.name=Test Person", "-c", "user.email=test@example.com", "merge", "-q", "--no-edit", "origin/main", cwd=wt)
    commit(wt, "c.txt", "fix: address the review (#9)")
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("failure")
    assert remote_head(origin) == git("rev-parse", "HEAD", cwd=wt)


def test_a_teammate_commit_on_top_of_the_branch_is_still_refused(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    rework_ready(A)
    before = remote_head(origin)
    commit(wt, "c.txt", "fix: address the review (#9)", ("Someone Else", "else@example.com"))
    st = A.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("authored" in e for e in st["failure"]["errors"])
    assert remote_head(origin) == before


def test_a_force_pushed_branch_is_adopted_and_reported(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    # the conflicts get resolved by hand and the branch is force-pushed
    seed = origin["seed"]
    git("fetch", "-q", "origin", cwd=seed)
    git("checkout", "-q", "-B", "pr", "origin/main", cwd=seed)
    commit(str(seed), "b.txt", "fix: keep the session alive, conflicts resolved (#9)")
    git("push", "-q", "-f", "origin", f"HEAD:{BRANCH}", cwd=seed)
    rewritten = remote_head(origin)

    st, errs = rework_ready(A)
    assert errs == [] and git("rev-parse", "HEAD", cwd=wt) == rewritten, "starts from what's on GitHub"
    assert st["prs"][0]["github_changes"]["rewritten"]
    assert "rewritten (force-pushed)" in A.github_changes_text(st)
    d = A.TICKETS / "DEMO-9"
    d.mkdir(parents=True, exist_ok=True)
    A.write_rework(d, st, st["rework_last"])
    assert "Since your last push, on GitHub" in (d / "rework.md").read_text()

    commit(wt, "c.txt", "fix: address the review (#9)")
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("failure")
    assert git("rev-parse", "HEAD~1", cwd=wt) == rewritten and "github_changes" not in st["prs"][0]


def test_unpushed_work_is_never_dropped_for_a_rewritten_branch(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    commit(wt, "mine.txt", "fix: not pushed yet (#9)")
    mine = git("rev-parse", "HEAD", cwd=wt)
    seed = origin["seed"]
    git("fetch", "-q", "origin", cwd=seed)
    git("checkout", "-q", "-B", "pr", "origin/main", cwd=seed)
    commit(str(seed), "b.txt", "fix: redone by hand (#9)")
    git("push", "-q", "-f", "origin", f"HEAD:{BRANCH}", cwd=seed)
    _, errs = rework_ready(A)
    assert any("rewritten on GitHub" in e for e in errs) and git("rev-parse", "HEAD", cwd=wt) == mine


def test_new_commits_on_github_reach_the_brief(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    seed = on_github(origin, ("r.txt", "fix: resolve conflicts (#9)"))
    git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=seed)
    st, errs = rework_ready(A)
    assert errs == [] and git("rev-parse", "HEAD", cwd=wt) == remote_head(origin)
    text = A.github_changes_text(st)
    assert "1 new commits" in text and "resolve conflicts" in text


def test_a_branch_held_by_another_worktree_is_left_alone(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    clone = origin["clone"]
    git("worktree", "remove", "--force", wt, cwd=clone)
    foreign = tmp_path / "someone-elses" / "pr-7"
    git("worktree", "add", "-q", str(foreign), BRANCH, cwd=clone)
    held = git("rev-parse", "HEAD", cwd=foreign)
    st = A.load("DEMO-9")
    st["prs"][0]["worktree"] = str(foreign)
    A.save(st)

    st, errs = rework_ready(A)
    assert errs == []
    own = Path(st["prs"][0]["worktree"])
    assert own.parent == A.WS / ".local" / "wt" and st["prs"][0]["detached"]
    commit(str(own), "c.txt", "fix: address the review (#9)")
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("failure")
    assert remote_head(origin) == git("rev-parse", "HEAD", cwd=own)
    assert git("rev-parse", "HEAD", cwd=foreign) == held and git("branch", "--show-current", cwd=foreign) == BRANCH


def test_a_fix_during_a_rework_reads_github_again(ticket_agent, origin, home, tmp_path, monkeypatch):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    rework_ready(A)
    # the run failed, then the conflicts are resolved by hand on GitHub, and Fix it says "check the latest"
    seed = on_github(origin, ("r.txt", "fix: resolve conflicts (#9)"))
    git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=seed)
    st = A.load("DEMO-9")
    st.update({"phase": "approved", "implement_session": "s1", "fix_request": {"instructions": "conflicts resolved, check the latest"}})
    A.save(st)
    seen = {}

    def fake_run(prompt, *a, **kw):
        seen["prompt"] = prompt
        return {}, "", {"session_id": "s1"}

    monkeypatch.setattr(A, "claude_run", fake_run)
    monkeypatch.setattr(A, "prepare_ticket", lambda key, st: A.TICKETS / key)
    (A.TICKETS / "DEMO-9").mkdir(parents=True, exist_ok=True)
    A.do_implement("DEMO-9")
    assert git("rev-parse", "HEAD", cwd=wt) == remote_head(origin), "caught up before the fix ran"
    p = seen["prompt"]
    assert "conflicts resolved, check the latest" in p and "resolve conflicts" in p and wt in p
    assert "/ticket-work rework" in p
    assert (A.TICKETS / "DEMO-9" / "rework.md").exists()
