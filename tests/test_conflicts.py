"""A PR the agent opened can have a merge conflict with its base branch. The fix is a merge of the base into the PR's own
branch, made in the agent's worktree, checked like any rework, and pushed fast-forward only. Nothing is force-pushed."""

import json
import subprocess

from conftest import git
from test_ticket_push import BRANCH, commit, remote_head, ticket, worktree


def opened(A, origin, home, tmp_path):
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"
    return wt


def base_moves_on_conflicting_line(origin, wt):
    """develop/main gets a change to the same file the PR changed: a real conflict when the merge runs."""
    seed = origin["seed"]
    git("fetch", "-q", "origin", cwd=seed)
    git("checkout", "-q", "-B", "main", "origin/main", cwd=seed)
    (seed / "b.txt").write_text("the base changed this line\n")
    git("add", ".", cwd=seed)
    git("-c", "user.name=Other", "-c", "user.email=other@example.com", "commit", "-qm", "chore: base moves on", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)


def test_a_conflict_is_reported_from_github(ticket_agent, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    view = {"state": "OPEN", "isDraft": True, "reviewDecision": "", "mergedAt": None, "headRefOid": "a", "title": "t", "url": "u",
            "updatedAt": "2026-01-02T00:00:00Z", "statusCheckRollup": [], "comments": [], "reviews": [],
            "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
    d = tmp_path / "canned"
    d.mkdir()
    (d / "pr_view_7").write_text(json.dumps(view))
    monkeypatch.setenv("FAKE_GH_DIR", str(d))
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7}]})
    A.sync_ticket_prs("DEMO-7")
    live = A.load("DEMO-7")["prs"][0]["live"]
    assert live["conflicts"] is True and live["merge_state"] == "DIRTY"
    view.update({"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"})
    (d / "pr_view_7").write_text(json.dumps(view))
    A.sync_ticket_prs("DEMO-7")
    assert A.load("DEMO-7")["prs"][0]["live"]["conflicts"] is False, "resolved on GitHub clears the flag"


def test_fix_conflict_merges_the_base_into_the_agents_own_branch_and_pushes_fast_forward(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    base_moves_on_conflicting_line(origin, wt)
    before = remote_head(origin)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open", "conflicts": True, "feedback": []}
    st.update({"fix_conflict": True, "conflict_request": {"instructions": "keep both lines", "at": "2026-10-20 10:00"}})
    A.save(st)
    assert A.prepare_conflict_fix(A.load("DEMO-9")) == []
    # the agent merges and resolves in its own worktree, then writes its manifest the way any run does
    subprocess.run(["git", "-C", wt, "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", wt, "merge", "-q", "--no-edit", "origin/main"], capture_output=True)
    (open(f"{wt}/b.txt", "w")).write("the base changed this line\nour fix\n")
    git("add", ".", cwd=wt)
    git("-c", "user.name=Test Person", "-c", "user.email=test@example.com", "commit", "-qm", "fix(DEMO-9): merge main, keep both (#9)", cwd=wt)
    st = A.load("DEMO-9")
    m = A.RUNS / "DEMO-9" / "r1" / "manifest.json"
    m.write_text(json.dumps({"verdict": "done", "summary": "merged main", "prs": [{"repo": "demo", "worktree": wt, "branch": BRANCH,
                                                                                   "base": "main", "title": "fix: keep the session alive (#9)",
                                                                                   "body": "keeps the session alive.", "order": 1, "changes": "merged main"}]}))
    st["manifest"], st["run_id"] = str(m), "r1"
    A.save(st)
    st = A.finalize_rework("DEMO-9")
    assert st["phase"] == "pr_open", st.get("error") or st.get("failure")
    assert remote_head(origin) == git("rev-parse", "HEAD", cwd=wt)
    assert remote_head(origin) != before, "the merge was pushed to the agent's own branch"
    assert not A.load("DEMO-9").get("fix_conflict"), "the request is cleared once it is pushed"


def test_a_merge_that_does_not_bring_the_base_in_is_refused(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    base_moves_on_conflicting_line(origin, wt)
    before = remote_head(origin)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open", "conflicts": True, "feedback": []}
    st.update({"fix_conflict": True, "conflict_request": {"instructions": "fix it", "at": "2026-10-20 10:00"}})
    A.save(st)
    A.prepare_conflict_fix(A.load("DEMO-9"))
    # a commit that is not a merge of the base: the fix did not fix the conflict
    commit(wt, "c.txt", "fix: something else (#9)")
    st = A.load("DEMO-9")
    m = A.RUNS / "DEMO-9" / "r1" / "manifest.json"
    m.write_text(json.dumps({"verdict": "done", "summary": "x", "prs": [{"repo": "demo", "worktree": wt, "branch": BRANCH, "base": "main",
                                                                          "title": "fix: keep the session alive (#9)", "body": "keeps it.", "order": 1}]}))
    st["manifest"], st["run_id"] = str(m), "r1"
    A.save(st)
    st = A.finalize_rework("DEMO-9")
    assert st["phase"] == "needs_attention"
    assert any("the conflict is still there" in e for e in st["failure"]["errors"]), st["failure"]["errors"]
    assert remote_head(origin) == before, "nothing that doesn't fix the conflict reaches the PR"


def test_a_conflict_fix_never_pushes_a_teammates_commit(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = opened(A, origin, home, tmp_path)
    base_moves_on_conflicting_line(origin, wt)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open", "conflicts": True, "feedback": []}
    st.update({"fix_conflict": True, "conflict_request": {"instructions": "fix it", "at": "2026-10-20 10:00"}})
    A.save(st)
    A.prepare_conflict_fix(A.load("DEMO-9"))
    git("-c", "user.name=Other", "-c", "user.email=other@example.com", "commit", "-q", "--allow-empty", "-m", "fix(DEMO-9): teammate (#9)", cwd=wt)
    st = A.load("DEMO-9")
    m = A.RUNS / "DEMO-9" / "r1" / "manifest.json"
    m.write_text(json.dumps({"verdict": "done", "summary": "x", "prs": [{"repo": "demo", "worktree": wt, "branch": BRANCH, "base": "main",
                                                                          "title": "fix: keep the session alive (#9)", "body": "keeps it.", "order": 1}]}))
    st["manifest"], st["run_id"] = str(m), "r1"
    A.save(st)
    st = A.finalize_rework("DEMO-9")
    assert st["phase"] == "needs_attention", "a teammate's commit on the agent's branch is refused"
    assert remote_head(origin) != git("rev-parse", "HEAD", cwd=wt), "and the teammate's commit is not pushed"


def test_a_fix_is_refused_when_github_shows_no_conflict(ticket_agent):
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7, "live": {"state": "open", "conflicts": False}}]})
    import pytest
    with pytest.raises(SystemExit) as e:
        A.request_conflict_fix("DEMO-7", "fix it")
    assert "doesn't show a conflict" in str(e.value)
    assert not A.load("DEMO-7").get("fix_conflict")


def test_a_fix_is_refused_for_a_merged_pr(ticket_agent):
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "merged", "prs": [{"repo": "demo", "number": 7, "live": {"state": "merged", "conflicts": True}}]})
    import pytest
    with pytest.raises(SystemExit):
        A.request_conflict_fix("DEMO-7", "fix it")


def test_a_fix_queues_a_run_that_merges_the_base(ticket_agent):
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "pr_open", "round": 1, "context": [],
            "prs": [{"repo": "demo", "number": 7, "branch": "dev/demo-7-x", "base": "main", "worktree": "/nowhere",
                     "live": {"state": "open", "conflicts": True}}]})
    msg = A.request_conflict_fix("DEMO-7", "keep the new retry")
    st = A.load("DEMO-7")
    assert st["fix_conflict"] is True and st["phase"] == "approved", msg
    task = A.conflict_task("DEMO-7", A.TICKETS / "DEMO-7", A.TICKETS / "x.json", st)
    assert "git merge --no-edit origin/main" in task and "keep the new retry" in task and "Never force" in task
