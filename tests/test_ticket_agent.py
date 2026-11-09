"""The ticket agent's decisions that don't need Claude: explanations, ordering, validation, slots, fixes, PR sync."""

import json
import re
import sys
import time

import pytest


def test_failure_explanations(ticket_agent):
    e = ticket_agent.explain
    assert "ran past its 150-minute limit" in e({"step": "implement", "timed_out": True, "timeout_min": 150})["title"]
    assert "budget" in e({"step": "implement", "subtype": "error_max_budget_usd", "budget": 40, "cost": 40})["title"]
    assert "stopped before it finished" in e({"step": "check", "stopped": True})["title"]
    assert "usage limit" in e({"step": "check", "result_text": "You've hit your session limit · resets 6pm"})["title"]
    assert "own code" in e({"step": "implement", "crash": "Traceback", "message": "KeyError: 'x'"})["title"]
    pr = e({"step": "pr", "errors": ["demo: commit abc1234 isn't authored and committed by X", "demo PR body: bold text"]})
    assert "wrong author" in pr["detail"] and "PR description" in pr["detail"] and pr.get("fix")


def test_usage_reset_time_is_parsed(ticket_agent):
    at = ticket_agent.usage_reset_at("You've hit your session limit · resets 6pm (UTC)")
    assert at and time.localtime(at).tm_hour == 18 and at > time.time() - 60
    assert ticket_agent.usage_reset_at("no time here") is None


def test_priority_and_sprint_order(ticket_agent):
    A = ticket_agent
    assert sorted(["P2", None, "P0", "High", "P1", "weird"], key=A.priority_rank) == ["P0", "P1", "P2", "High", "weird", None]
    today = time.strftime("%Y-%m-%d")
    sp = lambda s, d=7: {"title": s, "start": s, "days": d}
    current = {"title": "now", "start": today, "days": 7}
    order = sorted([None, sp("2001-01-01"), current, sp("2999-01-01")], key=A.sprint_rank)
    assert order[0] is current and order[1]["start"] == "2999-01-01" and order[-1] is None


def test_title_and_branch_formats(ticket_agent):
    A = ticket_agent
    t = A.title_re("DEMO-42")
    assert re.fullmatch(t, "fix: keep the session alive (#42)") and not re.fullmatch(t, "fix: other (#43)")
    b = A.branch_re("DEMO-42")
    assert re.fullmatch(b, "dev/demo-42-keep-alive") and not re.fullmatch(b, "fix/demo-42-x")
    assert not re.fullmatch(b, "dev/demo-42-")


def test_slots_and_queue_order(ticket_agent):
    A = ticket_agent
    data = A.RS.load(A.RUN_SETTINGS, A.JOBS)
    data["parallel"] = {"implement": 2, "check": 1}
    A.RS.save(A.RUN_SETTINGS, data)
    a, b, c = A.take_slot("implement"), A.take_slot("implement"), A.take_slot("implement")
    assert a and b and c is None and A.running_count("implement") == 2
    a.close(); b.close()
    today = time.strftime("%Y-%m-%d")
    for key, prio, sprint in (("DEMO-1", "P2", None), ("DEMO-2", "P0", {"title": "S", "start": today, "days": 7}),
                              ("DEMO-3", "P1", None)):
        A.save({"key": key, "phase": "approved", "priority": prio, "sprint": sprint})
    A.start_queued()
    assert [s[1] for s in A.spawned] == ["DEMO-2", "DEMO-3"], "current sprint P0 first, then P1, two slots"


def test_failure_then_fix_request(ticket_agent):
    A = ticket_agent
    A.save({"key": "DEMO-5", "phase": "implementing", "run_id": "r1", "implement_session": "s1"})
    st = A.record_failure(A.load("DEMO-5"), "implement", "the run wrote no manifest", {"session_id": "s1", "timed_out": True,
                                                                                         "timeout_min": 150})
    assert st["phase"] == "failed" and st["failure"]["explained"]["title"].startswith("The implementation ran past")
    msg = A.request_fix("DEMO-5", "run only the touched tests", "resume")
    st = A.load("DEMO-5")
    assert st["phase"] == "approved" and st["fix_request"]["instructions"] == "run only the touched tests" and msg
    prompt = A.fix_prompt(st, st["fix_request"], "implementation")
    assert "ran past" in prompt and "run only the touched tests" in prompt


def test_usage_limit_failure_is_not_diagnosed_and_resumes_after_reset(ticket_agent):
    A = ticket_agent
    A.CFG["diagnose"] = True
    A.save({"key": "DEMO-6", "phase": "implementing", "run_id": "r1", "implement_session": "s1"})
    A.record_failure(A.load("DEMO-6"), "implement", "limited", {"result_text": "You've hit your session limit · resets 6pm"})
    assert not [s for s in A.spawned if s[0] == "_diagnose"]
    st = A.load("DEMO-6")
    st["failure"]["retry_at"] = time.time() - 1
    A.save(st)
    A.heal_stopped_runs(paused=True)
    assert A.load("DEMO-6")["phase"] == "failed", "paused: no automatic resume"
    A.heal_stopped_runs(paused=False)
    assert A.load("DEMO-6")["phase"] == "approved"


def canned(fakes, monkeypatch, tmp_path, answers):
    d = tmp_path / "canned"
    d.mkdir(exist_ok=True)
    for key, value in answers.items():
        (d / key).write_text(json.dumps(value))
    monkeypatch.setenv("FAKE_GH_DIR", str(d))


def test_pr_sync_sets_phase_and_counts_human_feedback(ticket_agent, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    view = {"state": "OPEN", "isDraft": False, "reviewDecision": "CHANGES_REQUESTED", "mergedAt": None, "headRefOid": "abc",
            "title": "fix: x (#7)", "url": "u", "updatedAt": "2026-01-02T00:00:00Z",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}, {"status": "IN_PROGRESS"}],
            "comments": [{"author": {"login": "github-actions"}, "createdAt": "2026-01-02T00:00:00Z", "body": "CodeQL", "url": "x"}],
            "reviews": [{"author": {"login": "mate"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-01-02T00:00:00Z", "body": "fix the retry"}]}
    inline = [[{"user": {"login": "mate"}, "created_at": "2026-01-03T00:00:00Z", "body": "here", "path": "a.py", "line": 3},
               {"user": {"login": "tester"}, "created_at": "2026-01-03T00:00:00Z", "body": "my own reply", "path": "a.py", "line": 3}]]
    canned(fakes, monkeypatch, tmp_path, {"pr_view_7": view, "api_repos_acme_demo_pulls_7_comments": inline})
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7, "feedback_since": "2026-01-01T00:00:00Z"}]})
    A.sync_prs()
    st = A.load("DEMO-7")
    live = st["prs"][0]["live"]
    assert live["state"] == "open" and live["review"] == "changes_requested"
    assert live["checks"] == {"passed": 1, "failed": 1, "pending": 1, "skipped": 0}
    assert st["new_feedback"] == 2, "the review and the inline comment by a person; not the bot, not my own reply"
    assert [f["who"] for f in live["feedback"] if f["bot"]] == ["github-actions"]

    view.update({"state": "MERGED", "mergedAt": "2026-01-04T00:00:00Z"})
    canned(fakes, monkeypatch, tmp_path, {"pr_view_7": view})
    A.sync_prs()
    assert A.load("DEMO-7")["phase"] == "merged"


def test_pr_refresh_reads_one_ticket_and_follows_draft_to_merged(ticket_agent, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    view = {"state": "OPEN", "isDraft": True, "reviewDecision": "", "mergedAt": None, "headRefOid": "abc", "title": "t",
            "url": "u", "updatedAt": "2026-01-02T00:00:00Z", "statusCheckRollup": [], "comments": [], "reviews": []}
    canned(fakes, monkeypatch, tmp_path, {"pr_view_7": view})
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7}]})
    A.save({"key": "DEMO-8", "phase": "pr_open", "prs": [{"repo": "demo", "number": 8}]})
    assert A.prs_stale(A.load("DEMO-7")), "never read yet"

    assert A.sync_ticket_prs("DEMO-7") is True
    st = A.load("DEMO-7")
    assert st["prs"][0]["live"]["state"] == "draft"
    assert not A.prs_stale(st), "just read, so the desk doesn't read it again when the ticket opens"
    assert "live" not in A.load("DEMO-8")["prs"][0], "only the ticket asked for was read"

    # marked ready on GitHub, then merged: each refresh follows it
    view["isDraft"] = False
    canned(fakes, monkeypatch, tmp_path, {"pr_view_7": view})
    A.sync_ticket_prs("DEMO-7")
    assert A.load("DEMO-7")["prs"][0]["live"]["state"] == "open"
    view.update({"state": "MERGED", "mergedAt": "2026-01-04T00:00:00Z"})
    canned(fakes, monkeypatch, tmp_path, {"pr_view_7": view})
    A.sync_ticket_prs("DEMO-7")
    st = A.load("DEMO-7")
    assert st["prs"][0]["live"]["state"] == "merged" and st["phase"] == "merged"


def test_pr_refresh_keeps_what_it_knew_when_github_fails(ticket_agent, fakes, monkeypatch):
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "pr_open",
            "prs": [{"repo": "demo", "number": 7, "live": {"state": "draft", "feedback": []}}]})

    def boom(pr):
        raise RuntimeError("gh: HTTP 502")
    monkeypatch.setattr(A, "pr_live", boom)
    assert A.sync_ticket_prs("DEMO-7") is True
    pr = A.load("DEMO-7")["prs"][0]
    assert pr["live"]["state"] == "draft", "a failed read doesn't blank the last known state"
    assert "502" in pr["live_error"]
    assert A.load("DEMO-7")["phase"] == "pr_open"


def test_pr_refresh_leaves_running_and_unopened_tickets_alone(ticket_agent, monkeypatch):
    A = ticket_agent
    calls = []
    monkeypatch.setattr(A, "pr_live", lambda pr: calls.append(pr) or {"state": "open", "feedback": []})
    A.save({"key": "DEMO-1", "phase": "ready", "prs": [{"repo": "demo", "number": 1}]})
    A.save({"key": "DEMO-2", "phase": "pr_open", "prs": []})
    A.save({"key": "DEMO-3", "phase": "pr_open", "prs": [{"repo": "demo", "number": 3}]})
    monkeypatch.setattr(A, "is_running", lambda key: key == "DEMO-3")
    assert A.sync_prs() == 0 and calls == []


def test_pr_refresh_does_not_overwrite_a_change_made_while_github_was_read(ticket_agent, monkeypatch):
    """A click (rework) that lands during the GitHub read must win over the refresh."""
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7}]})

    def slow_read(pr):
        st = A.load("DEMO-7")
        st["phase"] = "approved"
        A.save(st)
        return {"state": "open", "feedback": []}
    monkeypatch.setattr(A, "pr_live", slow_read)
    assert A.sync_ticket_prs("DEMO-7") is False
    assert A.load("DEMO-7")["phase"] == "approved"


def test_ptk_prs_prints_each_pr_state(ticket_agent, fakes, monkeypatch, tmp_path, capsys):
    A = ticket_agent
    view = {"state": "OPEN", "isDraft": False, "reviewDecision": "APPROVED", "mergedAt": None, "headRefOid": "a",
            "title": "t", "url": "u", "updatedAt": "2026-01-02T00:00:00Z", "statusCheckRollup": [], "comments": [], "reviews": []}
    canned(fakes, monkeypatch, tmp_path, {"pr_view_7": view})
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7}]})
    A.cmd_prs("DEMO-7")
    out = capsys.readouterr().out
    assert "DEMO-7  demo #7  open, approved" in out


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_screenshots_are_saved_by_content_and_bad_files_are_refused(ticket_agent):
    A = ticket_agent
    name = A.save_attachment("DEMO-7", "Screen Shot (1).PNG", PNG)
    assert name.startswith("from-you-") and name.endswith(".png") and " " not in name
    assert (A.TICKETS / "DEMO-7" / "attachments" / name).read_bytes() == PNG
    assert A.save_attachment("DEMO-7", "b.jpg", b"\xff\xd8\xff\xe0" + b"0" * 8).endswith(".jpg")
    assert A.save_attachment("DEMO-7", "c.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ").endswith(".webp")
    for bad in (b"<svg onload=alert(1)>", b"#!/bin/sh\nrm -rf ~", b"%PDF-1.7", b""):
        with pytest.raises(SystemExit):
            A.save_attachment("DEMO-7", "x.png", bad)
    with pytest.raises(SystemExit):
        A.save_attachment("DEMO-7", "big.png", PNG + b"\x00" * 12_000_001)
    # the name can't leave the ticket's attachments folder, and a same-second twin doesn't overwrite
    a = A.save_attachment("DEMO-7", "../../../etc/passwd.png", PNG)
    b = A.save_attachment("DEMO-7", "../../../etc/passwd.png", PNG)
    assert a != b and {a, b} <= set(p.name for p in (A.TICKETS / "DEMO-7" / "attachments").iterdir())
    with pytest.raises(SystemExit):
        A.save_attachment("../DEMO-7", "x.png", PNG)


def test_rework_lists_screenshots_that_exist_and_ignores_the_rest(ticket_agent):
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7, "branch": "dev/demo-7-x",
                                                          "base": "main", "worktree": "/nowhere", "live": {"state": "open"}}]})
    shot = A.save_attachment("DEMO-7", "qa.png", PNG)
    A.request_rework("DEMO-7", "QA says it is still empty", [shot, "../../secrets.png", "missing.png"])
    st = A.load("DEMO-7")
    assert st["rework_request"]["images"] == [shot], "only a file in this ticket's attachments counts"

    d = A.TICKETS / "DEMO-7"
    A.write_rework(d, st, st["rework_request"])
    text = (d / "rework.md").read_text()
    assert f"attachments/{shot}" in text and "QA says it is still empty" in text

    # a follow-up PR (the PRs are merged) gets them too
    st["prs"][0]["live"] = {"state": "merged"}
    A.write_rework(d, st, st["rework_request"])
    assert f"attachments/{shot}" in (d / "rework.md").read_text()


def test_ptk_rework_image_flag_attaches_files(ticket_agent, tmp_path, monkeypatch, capsys):
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "pr_open", "prs": [{"repo": "demo", "number": 7, "live": {"state": "open"}}]})
    img = tmp_path / "qa shot.png"
    img.write_bytes(PNG)
    monkeypatch.setattr(sys, "argv", ["ptk", "rework", "DEMO-7", "still broken", "--image", str(img)])
    A.cmd_rework("DEMO-7")
    st = A.load("DEMO-7")
    assert len(st["rework_request"]["images"]) == 1 and st["rework_request"]["instructions"] == "still broken"
    monkeypatch.setattr(sys, "argv", ["ptk", "rework", "DEMO-7", "--image", str(tmp_path / "nope.png")])
    with pytest.raises(SystemExit):
        A.cmd_rework("DEMO-7")
    monkeypatch.setattr(sys, "argv", ["ptk", "rework", "DEMO-7", "x", "--image"])
    with pytest.raises(SystemExit):
        A.cmd_rework("DEMO-7")


class FakeTracker:
    """Stands in for the tracker: a fixed list from search(), nothing else."""
    name = "Jira"
    can_login = False
    format = "jira"

    def __init__(self, status):
        self.status = status

    def search(self):
        return [{"key": "DEMO-7", "summary": "Stop the retry loop", "type": "Bug", "status": self.status, "priority": "P1",
                 "updated": "2026-10-18T10:00:00.000+0000", "url": "https://x/7", "source": {"key": "DEMO-7"}}]

    def whoami(self):
        return {"ok": True, "name": "tester"}


def poll(A, monkeypatch, status):
    monkeypatch.setattr(A, "TR", FakeTracker(status))
    assert A.sync_tracker() is True
    return A.load("DEMO-7")


def test_a_ticket_moving_from_testing_back_to_work_is_flagged_once(ticket_agent, monkeypatch):
    A = ticket_agent
    A.CFG["active_statuses"] = ["To Do", "In Progress", "Reopened"]
    A.save({"key": "DEMO-7", "phase": "pr_open", "status": "Testing", "context": [], "prs": [{"repo": "demo", "number": 7}]})
    assert not poll(A, monkeypatch, "Testing").get("came_back"), "no move yet"
    assert not poll(A, monkeypatch, "In Review").get("came_back"), "moving between two out-of-hands statuses is not a bounce"
    st = poll(A, monkeypatch, "In Progress")
    assert st["came_back"], "Testing/In Review -> In Progress is QA sending it back"
    first = st["came_back"]
    assert poll(A, monkeypatch, "In Progress")["came_back"] == first, "the same move is not flagged again"
    # after a rework the desk clears it and records the status the new work was done under
    st = A.load("DEMO-7")
    st.update({"came_back": None})
    A.save(st)
    assert not poll(A, monkeypatch, "In Progress").get("came_back"), "still In Progress after the rework is not a new bounce"
    assert not poll(A, monkeypatch, "Testing").get("came_back")
    assert poll(A, monkeypatch, "Reopened")["came_back"], "a second bounce is flagged again"


def test_nothing_is_flagged_by_status_until_the_person_lists_the_working_statuses(ticket_agent, monkeypatch):
    A = ticket_agent
    A.CFG.pop("active_statuses", None)
    A.save({"key": "DEMO-7", "phase": "pr_open", "status": "Testing", "context": []})
    assert not poll(A, monkeypatch, "In Progress").get("came_back")
    assert A.came_back_move("Testing", "In Progress") is False


def test_a_ticket_that_left_the_list_and_returned_is_still_flagged(ticket_agent, monkeypatch):
    A = ticket_agent
    A.save({"key": "DEMO-7", "phase": "merged", "status": "Done", "gone": True, "context": []})
    assert poll(A, monkeypatch, "Reopened")["came_back"]


def test_a_ticket_without_prs_or_before_its_pr_is_never_flagged(ticket_agent, monkeypatch):
    A = ticket_agent
    A.CFG["active_statuses"] = ["In Progress"]
    A.save({"key": "DEMO-7", "phase": "ready", "status": "Testing", "context": []})
    assert not poll(A, monkeypatch, "In Progress").get("came_back")


def test_commit_ids_never_look_like_http_errors(ticket_agent):
    """A commit id like 4031abc once made a PR check failure read as 'model not allowed' (CI hit it by chance)."""
    e = ticket_agent.explain
    for sha in ("4031abc", "401beef", "5290aaa"):
        f = {"step": "pr", "message": f"demo: commit {sha} isn't authored and committed by X",
             "errors": [f"demo: commit {sha} isn't authored and committed by X"]}
        assert e(f)["title"] == "The PR checks failed, so nothing was pushed", sha
    assert "isn't allowed" in e({"step": "check", "result_text": "API Error: 403 team not allowed"})["title"]
    assert "sign in" in e({"step": "check", "result_text": "API Error: 401"})["title"]
