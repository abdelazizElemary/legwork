"""A PR the person opened by hand can become the ticket's PR: reworks then add commits to its own branch, never forced, and
every rework starts from the latest base branch. Everything the person wants goes through the one Tell box."""

import json
from pathlib import Path

import pytest

from conftest import KIT, git
from test_ticket_agent import canned
from test_ticket_push import commit, remote_head

HAND = "feature/by-hand"
VIEW = {"number": 7, "state": "OPEN", "author": {"login": "tester"}, "isCrossRepository": False, "headRefName": HAND,
        "baseRefName": "main", "headRefOid": "", "title": "perf: cache the parsed config (#9)", "body": "Opened by hand.",
        "url": "https://github.com/acme/demo/pull/7", "isDraft": False, "comments": [], "reviews": [], "statusCheckRollup": []}


def by_hand(origin):
    """A branch the person pushed and opened a PR from themselves, checked out in their own main checkout."""
    seed = origin["seed"]
    git("checkout", "-q", "-b", HAND, cwd=seed)
    commit(str(seed), "h.txt", "perf: cache the parsed config (#9)")
    git("push", "-q", "origin", HAND, cwd=seed)
    git("checkout", "-q", "main", cwd=seed)
    git("fetch", "-q", "origin", cwd=origin["clone"])
    git("checkout", "-q", HAND, cwd=origin["clone"])
    return remote_head(origin, HAND)


def failed_ticket(A, **kw):
    A.save({"key": "DEMO-9", "phase": "failed", "round": 2, "prs": [], "failed_step": "implement",
            "failure": {"id": "f1", "message": "blocked"}, "diagnosis": {"state": "done", "fix": {"action": "needs_you"}},
            "previous_prs": [{"repo": "demo", "number": 5, "url": "u5", "live": {"state": "closed"}}],
            "source": {"repo": "acme/demo", "number": 9, "ref": "acme/demo#9"}, **kw})


def link(A, fakes, monkeypatch, tmp_path, origin, **view):
    head = by_hand(origin)
    canned(fakes, monkeypatch, tmp_path, {"pr_view_7": {**VIEW, "headRefOid": head, **view}})
    return A.link_pr("DEMO-9", "acme/demo", 7)


def test_your_own_pr_becomes_the_tickets_pr(ticket_agent, origin, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    failed_ticket(A)
    out = link(A, fakes, monkeypatch, tmp_path, origin)
    st = A.load("DEMO-9")
    pr = st["prs"][0]
    assert "Linked #7" in out and st["phase"] == "pr_open" and not st.get("failure") and not st.get("diagnosis")
    assert st["implement_summary"].startswith("Linked #7"), "what the blocked run said no longer applies"
    assert (pr["branch"], pr["base"], pr["number"]) == (HAND, "main", 7) and pr["pushed_sha"] == remote_head(origin, HAND)


@pytest.mark.parametrize("view, why", [({"author": {"login": "mate"}}, "opened by mate"),
                                       ({"isCrossRepository": True}, "fork"),
                                       ({"state": "MERGED"}, "merged")])
def test_a_teammates_pr_a_fork_or_a_closed_pr_is_never_linked(ticket_agent, origin, fakes, monkeypatch, tmp_path, view, why):
    A = ticket_agent
    failed_ticket(A)
    with pytest.raises(SystemExit) as e:
        link(A, fakes, monkeypatch, tmp_path, origin, **view)
    assert why in str(e.value) and A.load("DEMO-9")["prs"] == []


def ready_rework(A):
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open"}
    errs = A.prepare_rework(st)
    st.update({"rework_mode": True, "rework_last": {"instructions": "cache the config once"}, "run_id": "r1",
               "manifest": str(A.RUNS / "DEMO-9" / "r1" / "manifest.json")})
    (A.RUNS / "DEMO-9" / "r1").mkdir(parents=True, exist_ok=True)
    Path(st["manifest"]).write_text(json.dumps({"verdict": "done", "summary": "the config is cached once",
                                                "prs": [{"repo": "demo", "changes": "one cache"}]}))
    A.save(st)
    return st, errs


def test_a_rework_commits_on_the_linked_branch_without_touching_your_checkout(ticket_agent, origin, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    failed_ticket(A)
    link(A, fakes, monkeypatch, tmp_path, origin)
    st, errs = ready_rework(A)
    assert errs == []
    wt = st["prs"][0]["worktree"]
    assert Path(wt).parent == A.WS / ".local" / "wt" and wt.endswith("-pr7") and st["prs"][0]["detached"]
    yours = git("rev-parse", "HEAD", cwd=origin["clone"])
    commit(wt, "t.txt", "perf: keep one cache (#9)")
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("failure")
    assert remote_head(origin, HAND) == git("rev-parse", "HEAD", cwd=wt), "pushed to the PR's own branch"
    assert git("rev-parse", "HEAD", cwd=origin["clone"]) == yours and git("branch", "--show-current", cwd=origin["clone"]) == HAND


def move_main(origin, name="m.txt", text="m\n"):
    seed = origin["seed"]
    git("checkout", "-q", "main", cwd=seed)
    git("pull", "-q", "origin", "main", cwd=seed)
    (seed / name).write_text(text)
    git("add", ".", cwd=seed)
    git("-c", "user.name=Someone Else", "-c", "user.email=else@example.com", "commit", "-qm", "feat: a teammate's change", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    return remote_head(origin, "main")


def test_a_rework_starts_from_the_latest_base(ticket_agent, origin, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    failed_ticket(A)
    link(A, fakes, monkeypatch, tmp_path, origin)
    tip = move_main(origin)
    st, errs = ready_rework(A)
    wt = st["prs"][0]["worktree"]
    assert errs == [] and st["prs"][0]["base_merge"]["merged"]
    assert A.is_ancestor(wt, tip, "HEAD"), "the latest main is merged in before the run starts"
    assert "merged the latest `main`" in A.github_changes_text(st)
    commit(wt, "t.txt", "perf: keep one cache (#9)")
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("failure")
    assert A.is_ancestor(wt, tip, remote_head(origin, HAND))


def test_a_base_that_conflicts_is_left_to_the_run_and_nothing_goes_out_without_it(ticket_agent, origin, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    failed_ticket(A)
    link(A, fakes, monkeypatch, tmp_path, origin)
    tip = move_main(origin, "h.txt", "the teammate's version\n")
    before = remote_head(origin, HAND)
    st, errs = ready_rework(A)
    wt = st["prs"][0]["worktree"]
    b = st["prs"][0]["base_merge"]
    assert errs == [] and not b["merged"] and b["conflicts"] == ["h.txt"]
    assert git("status", "--porcelain", cwd=wt) == "", "the failed merge was undone, the run starts clean"
    assert "conflicts in h.txt" in A.github_changes_text(st)
    commit(wt, "t.txt", "perf: keep one cache (#9)")
    st = A.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("isn't merged" in e for e in st["failure"]["errors"])
    assert remote_head(origin, HAND) == before

    # the run merges and resolves it: now it goes out
    git("-c", "user.name=Test Person", "-c", "user.email=test@example.com", "merge", "-q", "-X", "ours", "--no-edit", "origin/main", cwd=wt)
    st = A.load("DEMO-9")
    st["phase"] = "implementing"
    A.save(st)
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("failure")
    assert A.is_ancestor(wt, tip, remote_head(origin, HAND))


def propose(A, actions):
    st = A.load("DEMO-9")
    st["tell"] = {"state": "proposed", "message": "x", "id": "t1", "reply": "ok", "actions": actions}
    A.save(st)


def test_the_tell_box_does_what_the_other_boxes_did(ticket_agent):
    A = ticket_agent
    plan = A.clean_plan({"actions": [{"do": "link_pr", "repo": "acme/demo", "number": "7"}, {"do": "link_pr"},
                                     {"do": "fix", "instructions": "keep one cache", "how": "sideways"},
                                     {"do": "context", "text": "keep the old API"}, {"do": "approve"}]}, {})
    assert plan["actions"] == [{"do": "link_pr", "repo": "demo", "number": 7},
                               {"do": "fix", "instructions": "keep one cache", "how": "resume"},
                               {"do": "context", "text": "keep the old API"}, {"do": "approve"}]
    assert any("without a PR number" in d for d in plan["dropped"])

    A.save({"key": "DEMO-9", "phase": "needs_context", "context": [], "source": {"repo": "acme/demo", "number": 9}})
    propose(A, [{"do": "context", "text": "keep the old API"}, {"do": "approve"}])
    A.apply_tell("DEMO-9", "t1")
    st = A.load("DEMO-9")
    assert st["phase"] in ("approved", "implementing") and st["context"][-1]["text"] == "keep the old API"

    A.save({"key": "DEMO-9", "phase": "pr_open", "prs": [], "source": {"repo": "acme/demo", "number": 9}})
    propose(A, [{"do": "approve"}])
    with pytest.raises(SystemExit):
        A.apply_tell("DEMO-9", "t1")

    failed_ticket(A, phase="failed")
    propose(A, [{"do": "fix", "instructions": "keep one cache", "how": "resume"}])
    A.apply_tell("DEMO-9", "t1")
    assert A.load("DEMO-9")["fix_request"]["instructions"] == "keep one cache"


def test_the_ticket_desk_has_one_input_at_the_bottom():
    page = (KIT / "agents/ticket-agent/dashboard.html").read_text()
    assert page.count("<textarea") == 1 and 'id="tellText"' in page
    assert "${tellBox(view)}`;" in page, "the Tell box comes after the pane's body, pinned to its bottom"
    assert "position: sticky; bottom: 0" in page
    assert "needsYou ? \"\"" in page, "a failure that needs you isn't offered as Fix it again"


def test_the_composer_is_the_same_on_every_ticket():
    page = (KIT / "agents/ticket-agent/dashboard.html").read_text()
    box = page[page.index('<section class="tell-dock"'):page.index("</section>", page.index('<section class="tell-dock"'))]
    assert '<section class="tell-dock" aria-label' in box, "no state-dependent class that hides parts of it"
    composer = box[box.index('<div class="composer">'):]
    assert all(x in composer for x in ('id="tellText"', 'id="tellFiles"', 'data-act="tell"')), "text, attach and send in one row"
    assert "tell-acts" not in page and ".tell-dock.open" not in page
