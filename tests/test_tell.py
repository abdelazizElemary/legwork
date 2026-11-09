"""Telling the agent about a ticket in plain words. A read-only run reads the ticket, its PRs and the tracker and proposes a
plan made only of the desk's own actions; nothing changes until the person presses Do it. A claim like "I closed it" is
only taken when the tracker or GitHub agrees."""

import json

import pytest


def ticket(A, **kw):
    st = {"key": "DEMO-7", "phase": "pr_open", "status": "In Development", "context": [], "round": 1,
          "prs": [{"repo": "demo", "number": 7, "branch": "dev/demo-7-x", "base": "main", "worktree": "/nowhere",
                   "live": {"state": "open", "conflicts": False, "feedback": []}}], **kw}
    A.save(st)
    return st


def propose(A, plan):
    """What a finished tell run leaves: its plan on the ticket, waiting for the person."""
    st = A.load("DEMO-7")
    st["tell"] = {"state": "proposed", "message": "x", "id": "t1", **plan}
    A.save(st)


def test_a_tell_queues_a_read_only_run_and_keeps_the_message(ticket_agent):
    A = ticket_agent
    ticket(A)
    msg = A.request_tell("DEMO-7", "I resolved the conflict by hand, check the latest", images=[])
    st = A.load("DEMO-7")
    assert st["tell"]["state"] == "thinking" and st["tell"]["message"].startswith("I resolved")
    assert ("_tell", "DEMO-7") in A.spawned and "Reading" in msg
    with pytest.raises(SystemExit):
        A.request_tell("DEMO-7", "   ")


def test_only_the_desks_own_actions_can_be_proposed(ticket_agent):
    A = ticket_agent
    ticket(A)
    clean = A.clean_plan({"reply": "ok", "actions": [{"do": "park"}, {"do": "rm_rf"}, {"do": "push_force", "repo": "demo"},
                                                     {"do": "refresh_prs"}]}, A.load("DEMO-7"))
    assert [a["do"] for a in clean["actions"]] == ["park", "refresh_prs"]
    assert any("rm_rf" in d for d in clean["dropped"])


def test_do_it_runs_the_plan_and_parks_the_ticket(ticket_agent):
    A = ticket_agent
    ticket(A)
    propose(A, {"reply": "Parking it.", "actions": [{"do": "note", "text": "on hold until the API ships"}, {"do": "park"}]})
    out = A.apply_tell("DEMO-7", "t1")
    st = A.load("DEMO-7")
    assert st["phase"] == "skipped" and st["phase_before_skip"] == "pr_open"
    assert any("on hold until the API ships" in c["text"] for c in st["context"])
    assert st["tell"]["state"] == "done" and "Parked" in out


def test_a_plan_runs_once_and_a_stale_one_is_refused(ticket_agent):
    A = ticket_agent
    ticket(A)
    propose(A, {"reply": "ok", "actions": [{"do": "park"}]})
    A.apply_tell("DEMO-7", "t1")
    with pytest.raises(SystemExit):
        A.apply_tell("DEMO-7", "t1")
    ticket(A)
    propose(A, {"reply": "ok", "actions": [{"do": "park"}]})
    with pytest.raises(SystemExit):
        A.apply_tell("DEMO-7", "an-older-plan")


def test_closed_by_you_needs_the_tracker_or_github_to_agree(ticket_agent):
    A = ticket_agent
    ticket(A, status="In Development")
    propose(A, {"reply": "Marking it closed.", "actions": [{"do": "close"}]})
    with pytest.raises(SystemExit) as e:
        A.apply_tell("DEMO-7", "t1")
    assert "In Development" in str(e.value), "Jira still says it's open, so the desk doesn't drift from it"
    assert A.load("DEMO-7")["phase"] == "pr_open"

    ticket(A, status="Done")
    propose(A, {"reply": "Marking it closed.", "actions": [{"do": "close"}]})
    A.apply_tell("DEMO-7", "t1")
    assert A.load("DEMO-7")["phase"] == "closed"


def test_a_merged_pr_counts_as_closed_too(ticket_agent):
    A = ticket_agent
    ticket(A, status="In Development", prs=[{"repo": "demo", "number": 7, "live": {"state": "merged"}}])
    propose(A, {"reply": "ok", "actions": [{"do": "close"}]})
    A.apply_tell("DEMO-7", "t1")
    assert A.load("DEMO-7")["phase"] == "closed"


def test_fix_conflict_and_rework_go_through_their_own_checks(ticket_agent):
    A = ticket_agent
    ticket(A)
    propose(A, {"reply": "Fixing the conflict.", "actions": [{"do": "fix_conflict", "instructions": "keep both"}]})
    with pytest.raises(SystemExit) as e:
        A.apply_tell("DEMO-7", "t1")
    assert "doesn't show a conflict" in str(e.value), "GitHub must show the conflict, the plan alone isn't enough"
    st = A.load("DEMO-7")
    st["prs"][0]["live"]["conflicts"] = True
    A.save(st)
    propose(A, {"reply": "Fixing the conflict.", "actions": [{"do": "fix_conflict", "instructions": "keep both"}]})
    A.apply_tell("DEMO-7", "t1")
    st = A.load("DEMO-7")
    assert st["fix_conflict"] is True and st["conflict_request"]["instructions"] == "keep both"


def test_dismissing_a_plan_changes_nothing(ticket_agent):
    A = ticket_agent
    ticket(A)
    propose(A, {"reply": "ok", "actions": [{"do": "park"}]})
    A.dismiss_tell("DEMO-7")
    st = A.load("DEMO-7")
    assert st["phase"] == "pr_open" and st["tell"]["state"] == "dismissed"


def test_the_tell_run_reads_and_writes_nothing_but_its_plan(ticket_agent, monkeypatch):
    A = ticket_agent
    ticket(A)
    A.request_tell("DEMO-7", "close it for now", images=[])
    seen = {}

    def fake_run(prompt, budget, timeout_min, log_path, extra_dirs, job, st, resume=None, read_only=False):
        seen.update(job=job, read_only=read_only, prompt=prompt)
        out = A.TICKETS / "DEMO-7" / f"tell-{st['tell']['id']}.json"
        out.write_text(json.dumps({"reply": "I'll park it.", "understood": "Close it for now",
                                   "actions": [{"do": "park"}], "questions": []}))
        return {"total_cost_usd": 0.1}, "RESULT: written", {"session_id": "s1"}
    monkeypatch.setattr(A, "claude_run", fake_run)
    monkeypatch.setattr(A, "prepare_ticket", lambda key, st: A.TICKETS / key)
    (A.TICKETS / "DEMO-7").mkdir(parents=True, exist_ok=True)
    A.do_tell("DEMO-7")
    st = A.load("DEMO-7")
    assert seen["read_only"] is True and seen["job"] == "tell"
    assert "close it for now" in seen["prompt"]
    assert st["tell"]["state"] == "proposed" and st["tell"]["actions"] == [{"do": "park"}]
    assert st["phase"] == "pr_open", "the run itself changes nothing"


def test_a_closed_ticket_reopened_in_the_tracker_comes_back(ticket_agent, monkeypatch):
    A = ticket_agent
    A.CFG["active_statuses"] = ["In Progress", "Reopened"]

    class T:
        name, can_login, format = "Jira", False, "jira"
        def __init__(self, status): self.status = status
        def search(self):
            return [{"key": "DEMO-7", "summary": "x", "type": "Bug", "status": self.status, "priority": "P1",
                     "updated": "2026-10-21T10:00:00Z", "url": "u", "source": {"key": "DEMO-7"}}]
        def whoami(self): return {"ok": True, "name": "t"}
    A.save({"key": "DEMO-7", "phase": "closed", "status": "Done", "context": [], "prs": [{"repo": "demo", "number": 7}]})
    monkeypatch.setattr(A, "TR", T("Done"))
    A.sync_tracker()
    assert A.load("DEMO-7")["phase"] == "closed", "still done in Jira, still closed"
    monkeypatch.setattr(A, "TR", T("Reopened"))
    A.sync_tracker()
    st = A.load("DEMO-7")
    assert st["phase"] == "pr_open" and st["came_back"], "reopened in Jira, back on the desk"


def test_every_exchange_is_kept_in_full(ticket_agent):
    A = ticket_agent
    ticket(A)
    first = "this was addressed on #42\nI want to continue on the same PR, " + "x" * 200
    A.request_tell("DEMO-7", first, images=[])
    st = A.load("DEMO-7")
    st["tell"].update(state="done", done=["Rework started."])
    A.save(st)
    A.request_tell("DEMO-7", "check the latest", images=[])
    st = A.load("DEMO-7")
    assert [t["message"] for t in st["tell_history"]] == [first] and st["tell_history"][0]["state"] == "done"
    assert st["tell"]["message"] == "check the latest"
    assert [t["message"] for t in A.tell_thread(st, [])] == [first, "check the latest"]


def test_exchanges_from_before_come_back_from_the_log(ticket_agent):
    A = ticket_agent
    st = ticket(A)
    d = A.TICKETS / "DEMO-7"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tell-20260105-100012.json").write_text(json.dumps({"reply": "PR #42 is open.", "actions": [{"do": "rework"}]}))
    long = "y" * 120
    lines = ["2026-01-05 10:00 DEMO-7 tell: this was addressed on #42", "I want to continue",
             "2026-01-05 10:01 DEMO-7 tell plan: rework", "2026-01-05 10:02 DEMO-8 tell: someone else's ticket",
             "2026-01-06 09:30 DEMO-7 tell done: Noted; Rework started.",
             f"2026-01-06 09:45 DEMO-7 tell: {long}"]
    st["tell"] = {"state": "proposed", "id": "20260106-120000", "message": "the newest, in full", "at": "2026-01-06 12:00"}
    thread = A.tell_thread(st, lines)
    assert [t["at"] for t in thread] == ["2026-01-05 10:00", "2026-01-06 09:45", "2026-01-06 12:00"]
    old = thread[0]
    assert old["message"] == "this was addressed on #42\nI want to continue" and not old["partial"]
    assert old["state"] == "done" and old["done"] == ["Noted", "Rework started."] and old["reply"] == "PR #42 is open."
    assert thread[1]["partial"] and thread[1]["state"] == "proposed", "the log keeps only 120 characters"
    assert thread[2]["message"] == "the newest, in full"


def test_the_desk_shows_the_whole_conversation():
    from conftest import KIT
    page = (KIT / "agents/ticket-agent/dashboard.html").read_text()
    assert "function exchange(t, v, current)" in page and 'data-act="tell-earlier"' in page
    assert '"tell_thread": A.tell_thread(st, lines)' in (KIT / "agents/ticket-agent/dashboard.py").read_text()
