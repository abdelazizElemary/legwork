"""A rework answers review comments where they were left: in the thread for inline comments, quoting the comment for
the others. Never in the PR description."""

import json

from conftest import git
from test_ticket_agent import canned
from test_ticket_push import commit, remote_head, ticket, worktree

VIEW = {"state": "OPEN", "isDraft": False, "reviewDecision": "CHANGES_REQUESTED", "mergedAt": None, "headRefOid": "abc",
        "title": "fix: keep the session alive (#9)", "url": "u", "updatedAt": "2026-01-02T00:00:00Z", "statusCheckRollup": [],
        "comments": [{"id": "IC_1", "author": {"login": "mate"}, "createdAt": "2026-01-02T00:00:00Z",
                      "body": "Could this retry by itself instead of failing the whole request?",
                      "url": "https://github.com/acme/demo/pull/7#issuecomment-4242"},
                     {"id": "IC_2", "author": {"login": "github-actions"}, "createdAt": "2026-01-02T00:00:00Z",
                      "body": "CodeQL: 1 finding", "url": "https://github.com/acme/demo/pull/7#issuecomment-4343"}],
        "reviews": [{"id": "PRR_9", "author": {"login": "mate"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-01-02T00:00:00Z",
                     "body": "please handle the retry"}]}
INLINE = [[{"id": 555, "user": {"login": "mate"}, "created_at": "2026-01-03T00:00:00Z", "body": "this swallows the error",
            "path": "a.py", "line": 3},
           {"id": 556, "in_reply_to_id": 555, "user": {"login": "mate"}, "created_at": "2026-01-03T01:00:00Z",
            "body": "and it hides the timeout", "path": "a.py", "line": 3}]]


def live(A, fakes, monkeypatch, tmp_path, number=7):
    canned(fakes, monkeypatch, tmp_path, {f"pr_view_{number}": VIEW, f"api_repos_acme_demo_pulls_{number}_comments": INLINE})
    return A.pr_live({"repo": "demo", "number": number})


def test_every_comment_gets_an_id_a_reply_can_answer(ticket_agent, fakes, monkeypatch, tmp_path):
    fb = {f["id"]: f for f in live(ticket_agent, fakes, monkeypatch, tmp_path)["feedback"]}
    assert set(fb) == {"comment-4242", "comment-4343", "review-PRR_9", "inline-555", "inline-556"}
    assert fb["inline-556"]["thread"] == 555, "a reply in a thread answers the thread's first comment"
    assert fb["comment-4343"]["bot"]


def state_with(A, fakes, monkeypatch, tmp_path, **extra):
    feedback = live(A, fakes, monkeypatch, tmp_path)
    st = {"key": "DEMO-9", "prs": [{"repo": "demo", "number": 7, "live": feedback}], **extra}
    return st


def errors(A, st, m):
    import sys
    sys.path.insert(0, str(A.REVIEW_SKILL / "scripts"))
    from post_review import lint_text
    return A.reply_errors(st, m, lint_text)


def test_replies_are_checked(ticket_agent, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    st = state_with(A, fakes, monkeypatch, tmp_path)
    ok = {"repo": "demo", "pr": 7, "to": "comment-4242", "body": "they do now: a failed step retries up to 3 times (a.py:12)"}
    assert errors(A, st, {"replies": [ok]}) == []
    cases = {
        "isn't one of this ticket's PRs": {**ok, "pr": 99},
        "no comment": {**ok, "to": "comment-1"},
        "bot's report": {**ok, "to": "comment-4343"},
        "empty reply": {**ok, "body": " "},
        "attribution": {**ok, "body": "Generated with Claude Code"},
    }
    for needle, reply in cases.items():
        assert any(needle in e for e in errors(A, st, {"replies": [reply]})), needle
    assert any("second reply" in e for e in errors(A, st, {"replies": [ok, ok]}))


def test_the_pr_description_cannot_answer_a_reviewer(ticket_agent, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    st = state_with(A, fakes, monkeypatch, tmp_path)
    quoting = "Fixes the stuck state. Could this retry by itself instead of failing the whole request? Yes."
    talking = "Fixes the stuck state. @mate it retries now."
    for body in (quoting, talking):
        errs = errors(A, st, {"prs": [{"repo": "demo", "body": body}]})
        assert any("reply to the comment instead" in e for e in errs), body
    assert errors(A, st, {"prs": [{"repo": "demo", "body": "Fixes the stuck state: a failed step retries."}]}) == []


def test_rework_pushes_then_replies_in_place(ticket_agent, origin, home, tmp_path, fakes, monkeypatch):
    A = ticket_agent
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"

    feedback = live(A, fakes, monkeypatch, tmp_path)
    canned(fakes, monkeypatch, tmp_path, {
        "api_POST_repos_acme_demo_pulls_7_comments_555_replies": {"html_url": "https://github.com/acme/demo/pull/7#r1"},
        "api_POST_repos_acme_demo_issues_7_comments": {"html_url": "https://github.com/acme/demo/pull/7#c1"}})
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = feedback
    assert A.prepare_rework(st) == []
    st.update({"rework_mode": True, "rework_last": {"instructions": "answer the review"}})
    A.save(st)
    commit(wt, "c.txt", "fix: retry instead of failing (#9)")
    m = json.loads((tmp_path / "manifest.json").read_text())
    m["replies"] = [{"repo": "demo", "pr": 7, "to": "inline-556", "body": "it rethrows now, `a.py:3`"},
                    {"repo": "demo", "pr": 7, "to": "comment-4242", "body": "they do now, up to 3 times before failing"}]
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    fakes["log"].write_text("")

    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open" and remote_head(origin) == git("rev-parse", "HEAD", cwd=wt)
    calls = [json.loads(l) for l in fakes["log"].read_text().splitlines()]
    thread = [c for c in calls if "repos/acme/demo/pulls/7/comments/555/replies" in c]
    quote = [c for c in calls if "repos/acme/demo/issues/7/comments" in c]
    assert thread and "body=it rethrows now, `a.py:3`" in thread[0], "inline: in its thread, on the first comment"
    assert quote and quote[0][-1].startswith("body=> Could this retry") and "up to 3 times" in quote[0][-1]
    assert not [c for c in calls if c[:2] == ["pr", "edit"]], "the description was left alone"
    last = st["reworks"][-1]
    assert [r["to"] for r in last["replies"]] == ["inline-556", "comment-4242"] and not last["replies_failed"]
    assert st["prs"][0]["replied"]["comment-4242"] == "https://github.com/acme/demo/pull/7#c1"


def test_a_bad_reply_stops_the_push(ticket_agent, origin, home, tmp_path, fakes, monkeypatch):
    A = ticket_agent
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt)
    A.finalize("DEMO-9")
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = live(A, fakes, monkeypatch, tmp_path)
    A.prepare_rework(st)
    st["rework_mode"] = True
    A.save(st)
    before = remote_head(origin)
    commit(wt, "c.txt", "fix: retry instead of failing (#9)")
    m = json.loads((tmp_path / "manifest.json").read_text())
    m["replies"] = [{"repo": "demo", "pr": 7, "to": "comment-9999", "body": "done"}]
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    st = A.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("no comment" in e for e in st["failure"]["errors"])
    assert remote_head(origin) == before, "nothing pushed"


def test_a_follow_up_fills_in_the_new_pr_link(ticket_agent, fakes, monkeypatch, tmp_path):
    A = ticket_agent
    st = state_with(A, fakes, monkeypatch, tmp_path)
    fakes["log"].write_text("")
    posted, failed = A.post_replies(st, {"replies": [{"repo": "demo", "pr": 7, "to": "comment-4242",
                                                     "body": "handled in {new_pr}"}]},
                                    {"demo": "https://github.com/acme/demo/pull/8"})
    body = [json.loads(l) for l in fakes["log"].read_text().splitlines()][-1][-1]
    assert "handled in https://github.com/acme/demo/pull/8" in body and posted and not failed
