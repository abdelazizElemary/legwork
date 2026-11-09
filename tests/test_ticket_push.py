"""What reaches GitHub from the ticket agent: a fresh draft PR, and rework on the same PR. A local bare repo stands in
for GitHub, and the fake gh answers `gh pr create`."""

import json
import subprocess

from conftest import git

BRANCH = "dev/demo-9-keep-alive"


def worktree(home, origin):
    script = home / ".claude/skills/ticket-work/scripts/ticket_worktree.sh"
    out = subprocess.run(["bash", str(script), str(origin["clone"]), "DEMO-9", "fix", "keep-alive", "--base", "main",
                          "--branch-format", "dev/{key}-{slug}"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    info = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    assert info["BRANCH"] == BRANCH
    return info["WORKTREE"]


def commit(wt, name, msg, author=("Test Person", "test@example.com")):
    with open(f"{wt}/{name}", "w") as f:
        f.write(name + "\n")
    git("add", ".", cwd=wt)
    git("-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}", "commit", "-qm", msg, cwd=wt)


def ticket(A, tmp_path, wt, **extra):
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps({"verdict": "done", "summary": "keeps it alive", "prs": [
        {"repo": "demo", "worktree": wt, "branch": BRANCH, "base": "main", "title": "fix: keep the session alive (#9)",
         "body": "keeps the session alive.", "order": 1}]}))
    A.save({"key": "DEMO-9", "phase": "implementing", "run_id": "r1", "manifest": str(m),
            "source": {"repo": "acme/demo", "number": 9, "ref": "acme/demo#9"}, **extra})
    (A.RUNS / "DEMO-9" / "r1").mkdir(parents=True, exist_ok=True)
    return m


def remote_head(origin, branch=BRANCH):
    out = subprocess.run(["git", "--git-dir", str(origin["bare"]), "rev-parse", "--verify", "-q", branch],
                         capture_output=True, text=True)
    return out.stdout.strip() or None


def test_fresh_pr_is_checked_pushed_and_opened_as_draft(ticket_agent, origin, home, tmp_path, fakes):
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(ticket_agent, tmp_path, wt)
    st = ticket_agent.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("error")
    assert remote_head(origin) == git("rev-parse", "HEAD", cwd=wt)
    created = [json.loads(l) for l in fakes["log"].read_text().splitlines() if l.startswith('["pr", "create"')]
    assert created and "--draft" in created[0] and BRANCH in created[0]
    assert st["prs"][0]["url"].endswith("/pull/7") and st["prs"][0]["feedback_since"]


def test_someone_elses_commit_is_refused_and_nothing_is_pushed(ticket_agent, origin, home, tmp_path):
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)", author=("Someone Else", "else@example.com"))
    ticket(ticket_agent, tmp_path, wt)
    st = ticket_agent.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and "author" in st["failure"]["explained"]["detail"]
    assert remote_head(origin) is None


def test_commit_must_name_the_ticket(ticket_agent, origin, home, tmp_path):
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive")
    ticket(ticket_agent, tmp_path, wt)
    st = ticket_agent.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("mention" in e for e in st["failure"]["errors"])


def test_rework_fast_forwards_pushes_on_top_and_never_rewrites(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt = worktree(home, origin)
    commit(wt, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"

    # someone commits a suggestion on GitHub: the PR branch moves ahead of the worktree
    seed = origin["seed"]
    git("fetch", "-q", "origin", cwd=seed)
    git("checkout", "-q", "-b", "pr", f"origin/{BRANCH}", cwd=seed)
    commit(str(seed), "s.txt", "fix: apply suggestion (#9)")
    git("push", "-q", "origin", f"HEAD:{BRANCH}", cwd=seed)

    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open"}
    assert A.prepare_rework(st) == []
    A.save(st)
    assert git("rev-parse", "HEAD", cwd=wt) == remote_head(origin), "caught up with GitHub first"

    commit(wt, "c.txt", "fix: address the review (#9)")
    st = A.load("DEMO-9")
    st.update({"rework_mode": True, "rework_last": {"instructions": "address it"}})
    A.save(st)
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open" and remote_head(origin) == git("rev-parse", "HEAD", cwd=wt)
    assert st["reworks"][-1]["prs"]["demo"]["commits"] == 1

    # rewriting a pushed commit is refused, the PR branch stays as it was
    before = remote_head(origin)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open"}
    A.prepare_rework(st)
    st["rework_mode"] = True
    A.save(st)
    git("reset", "-q", "--hard", "HEAD~1", cwd=wt)
    commit(wt, "d.txt", "fix: rewritten (#9)")
    st = A.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("force" in e for e in st["failure"]["errors"])
    assert remote_head(origin) == before


# ---------- follow-up rounds: a new branch each time, stacked on an open PR, never a commit on the earlier one ----------

def round_cfg(A, new=True):
    A.CFG["rework_branch"] = "new" if new else "same"


def worktree2(home, origin, slug, *flags):
    script = home / ".claude/skills/ticket-work/scripts/ticket_worktree.sh"
    out = subprocess.run(["bash", str(script), str(origin["clone"]), "DEMO-9", "fix", slug, "--branch-format", "dev/{key}-{slug}", *flags],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    info = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    return info["WORKTREE"], info["BRANCH"]


def manifest2(A, tmp_path, wt, branch, base, run_id="r2"):
    m = tmp_path / "manifest2.json"
    m.write_text(json.dumps({"verdict": "done", "summary": "round two", "prs": [
        {"repo": "demo", "worktree": wt, "branch": branch, "base": base, "title": "fix: cover the 413 case (#9)",
         "body": "Follow-up to #7: the first PR missed the 413 case.", "order": 1}]}))
    (A.RUNS / "DEMO-9" / run_id).mkdir(parents=True, exist_ok=True)
    return m


def test_second_round_gets_its_own_worktree_and_branch(home, origin):
    first, b1 = worktree2(home, origin, "first-try", "--base", "main")
    second, b2 = worktree2(home, origin, "second-try", "--base", "main", "--round", "2")
    assert first != second and b1 != b2 and second.endswith("-r2")
    assert git("branch", "--show-current", cwd=first) == b1 and git("branch", "--show-current", cwd=second) == b2


def test_round_and_base_flags_are_checked(home, origin):
    script = home / ".claude/skills/ticket-work/scripts/ticket_worktree.sh"
    base = [str(origin["clone"]), "DEMO-9", "fix", "x-y"]
    for flags in (["--round", "two"], ["--round"], ["--base", "bad branch"], ["--base", "$(touch /tmp/pwned)"]):
        out = subprocess.run(["bash", str(script), *base, *flags], capture_output=True, text=True)
        assert out.returncode != 0, flags


def test_a_follow_up_stacks_on_the_open_pr_and_opens_a_new_draft_pr(ticket_agent, origin, home, tmp_path, fakes):
    A = ticket_agent
    wt1 = worktree(home, origin)
    commit(wt1, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt1)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"
    first_head = remote_head(origin)

    # the user asks for more work, the PR is still open: round 2 starts on a new branch and the PR is untouched
    round_cfg(A)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open"}
    A.save(st)
    st["rework_last"] = {"instructions": "also cover 413"}
    A.start_round(st)
    A.save(st)
    st = A.load("DEMO-9")
    assert st["round"] == 2 and st["prs"] == [] and st["previous_prs"][0]["number"] == 7 and st["previous_prs"][0]["round"] == 1

    wt2, b2 = worktree2(home, origin, "cover-413", "--base", BRANCH, "--round", "2")
    commit(wt2, "c.txt", "fix: cover the 413 case (#9)")
    m = manifest2(A, tmp_path, wt2, b2, BRANCH)
    st["manifest"], st["run_id"] = str(m), "r2"
    A.save(st)
    st = A.finalize("DEMO-9")
    assert st["phase"] == "pr_open", st.get("error")
    assert remote_head(origin) == first_head, "the earlier PR's branch was not touched"
    assert remote_head(origin, b2) == git("rev-parse", "HEAD", cwd=wt2)
    created = [json.loads(l) for l in fakes["log"].read_text().splitlines() if l.startswith('["pr", "create"')]
    assert len(created) == 2 and "--draft" in created[1] and created[1][created[1].index("--base") + 1] == BRANCH


def test_a_pr_cannot_be_based_on_just_any_branch(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt1 = worktree(home, origin)
    commit(wt1, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt1)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"
    git("push", "-q", "origin", "HEAD:refs/heads/someone-elses-work", cwd=wt1)
    round_cfg(A)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open"}
    A.start_round(st)
    wt2, b2 = worktree2(home, origin, "cover-413", "--base", "someone-elses-work", "--round", "2")
    commit(wt2, "c.txt", "fix: cover the 413 case (#9)")
    st["manifest"], st["run_id"] = str(manifest2(A, tmp_path, wt2, b2, "someone-elses-work")), "r2"
    A.save(st)
    st = A.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("base must be" in e for e in st["failure"]["errors"])
    assert remote_head(origin, b2) is None


def test_a_merged_earlier_pr_means_the_follow_up_starts_from_the_default_branch(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt1 = worktree(home, origin)
    commit(wt1, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt1)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"
    round_cfg(A)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "merged"}
    A.start_round(st)
    A.save(st)
    wt2, b2 = worktree2(home, origin, "cover-413", "--base", "main", "--round", "2")
    commit(wt2, "c.txt", "fix: cover the 413 case (#9)")
    st = A.load("DEMO-9")
    st["manifest"], st["run_id"] = str(manifest2(A, tmp_path, wt2, b2, "main")), "r2"
    A.save(st)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"
    # a stack on the merged branch is not allowed any more
    st = A.load("DEMO-9")
    errs = A.validate(st, {"repo": "demo", "worktree": wt2, "branch": b2, "base": BRANCH, "title": "fix: cover the 413 case (#9)", "body": "Follow-up to #7: more."})
    assert any("base must be" in e for e in errs)


def test_the_base_branch_disappearing_is_explained_not_a_crash(ticket_agent, origin, home, tmp_path):
    A = ticket_agent
    wt1 = worktree(home, origin)
    commit(wt1, "b.txt", "fix: keep the session alive (#9)")
    ticket(A, tmp_path, wt1)
    assert A.finalize("DEMO-9")["phase"] == "pr_open"
    round_cfg(A)
    st = A.load("DEMO-9")
    st["prs"][0]["live"] = {"state": "open"}
    A.start_round(st)
    wt2, b2 = worktree2(home, origin, "cover-413", "--base", BRANCH, "--round", "2")
    commit(wt2, "c.txt", "fix: cover the 413 case (#9)")
    git("--git-dir", str(origin["bare"]), "branch", "-D", BRANCH)     # the earlier PR was merged and its branch deleted
    st["manifest"], st["run_id"] = str(manifest2(A, tmp_path, wt2, b2, BRANCH)), "r2"
    A.save(st)
    st = A.finalize("DEMO-9")
    assert st["phase"] == "needs_attention" and any("is gone from GitHub" in e for e in st["failure"]["errors"])


def test_retrying_a_follow_up_does_not_start_another_round(ticket_agent):
    A = ticket_agent
    A.save({"key": "DEMO-9", "phase": "needs_attention", "round": 2, "prs": [], "failed_step": "implement",
            "rework_last": {"instructions": "cover 413"}, "previous_prs": [{"repo": "demo", "number": 7, "round": 1}],
            "failure": {"id": "f", "step": "implement", "message": "x"}})
    A.CFG["rework_branch"] = "new"
    assert A.in_followup(A.load("DEMO-9"))
    A.request_fix("DEMO-9", "try again", "rerun")
    st = A.load("DEMO-9")
    assert st["rework_request"]["instructions"].startswith("cover 413") and st["phase"] in ("approved", "implementing")
    st["rework_last"] = st["rework_request"]
    A.start_round(st)
    assert st["round"] == 2 and len(st["previous_prs"]) == 1, "no PRs opened yet, so nothing moves and the round stays 2"


def test_rework_md_for_a_follow_up_lists_earlier_prs_and_where_to_start(ticket_agent):
    A = ticket_agent
    A.CFG["rework_branch"] = "new"
    st = {"key": "DEMO-9", "round": 2, "prs": [], "rework_mode": False, "previous_prs": [
        {"repo": "demo", "number": 7, "round": 1, "branch": BRANCH, "url": "https://x/7", "live": {"state": "open"}},
        {"repo": "other", "number": 3, "round": 1, "branch": "dev/demo-9-other", "url": "https://x/3", "live": {"state": "merged"}}]}
    d = A.TICKETS / "DEMO-9"
    d.mkdir(parents=True, exist_ok=True)
    A.write_rework(d, st, {"instructions": "also 413", "images": []})
    text = (d / "rework.md").read_text()
    assert "Follow-up round 2" in text and "#7" in text and "#3" in text
    assert f"--base {BRANCH} --round 2" in text, "the open PR is stacked on"
    assert "other: the earlier PR is still open" not in text, "the merged one is not stacked on"
    assert "--round 2" in text and "Follow-up to #" in text


def test_new_comments_since_the_last_push_reach_rework_md(ticket_agent):
    A = ticket_agent
    d = A.TICKETS / "DEMO-9"
    d.mkdir(parents=True, exist_ok=True)
    (d / "issue.json").write_text(json.dumps({"comments": [
        {"author": "qa", "created": "2026-10-10T08:00:00.000+0000", "body": "old note"},
        {"author": "qa", "created": "2026-10-13T08:00:00Z", "body": "still fails on 413"}]}))
    since = A.to_epoch("2026-10-12T00:00:00Z")
    assert [c["body"] for c in A.comments_since(d, since)] == ["still fails on 413"]
    assert A.comments_since(d, 0) == [] and A.comments_since(d / "nope", since) == []
    assert A.to_epoch("2026-10-12T23:25:12.000-0500") == A.to_epoch("2026-10-13T04:25:12Z")
