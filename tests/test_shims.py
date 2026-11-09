"""The gh and git guards put in front of every unattended run. A blocked call must never reach the real binary."""

import json
import os
import subprocess
import sys

import pytest

from conftest import config, install, write_exe

RECORDER = '#!/bin/sh\necho "$@" >> "$SHIM_LOG"\n'


@pytest.fixture
def guarded(tmp_path, fakes):
    real = tmp_path / "real"
    real.mkdir()
    gh, git = write_exe(real / "gh", RECORDER), write_exe(real / "git", RECORDER)
    cfg = config(tmp_path, fakes)
    cfg["binaries"].update({"gh": str(gh), "git": str(git)})
    home = install(tmp_path, cfg)
    ws = tmp_path / "ws"
    (ws / "repo" / ".git").mkdir(parents=True)
    (ws / ".local" / "wt" / "demo-x").mkdir(parents=True)
    log = tmp_path / "shim.log"
    shims = home / ".claude/agent-lib/shims"

    def run(tool, args, cwd=None, env=None):
        log.write_text("")
        out = subprocess.run([sys.executable, str(shims / tool), *args], cwd=cwd or tmp_path, capture_output=True,
                             text=True, env={**os.environ, "SHIM_LOG": str(log), **(env or {})})
        reached = bool(log.read_text().strip())
        return out.returncode, out.stderr, reached
    return {"run": run, "ws": ws, "tmp": tmp_path}


GH_BLOCKED = [
    ["pr", "review", "1", "--approve"], ["pr", "comment", "1", "-b", "x"], ["pr", "merge", "1"], ["pr", "create"],
    ["pr", "edit", "1"], ["pr", "ready", "1"], ["issue", "comment", "1", "-b", "x"], ["issue", "edit", "1"],
    ["issue", "close", "1"], ["api", "-X", "POST", "repos/a/b/issues"], ["api", "-XPOST", "repos/a/b/issues"],
    ["api", "--method=PATCH", "repos/a/b"], ["api", "--method", "DELETE", "repos/a/b"],
    ["api", "repos/a/b/issues", "-f", "title=x"], ["api", "repos/a/b/issues", "--input", "body.json"],
    ["api", "graphql", "-f", "query=mutation { addComment }"], ["repo", "delete", "a/b"], ["repo", "edit", "a/b"],
    ["auth", "refresh"], ["auth", "logout"], ["extension", "install", "x/y"], ["project", "item-edit"],
    ["label", "create", "x"], ["release", "create", "v1"], ["workflow", "run", "x"], ["secret", "set", "x"],
    ["ruleset", "delete", "1"], ["gist", "create", "f"]]
GH_ALLOWED = [
    ["pr", "view", "1"], ["pr", "diff", "1"], ["pr", "list"], ["issue", "view", "1"], ["search", "prs", "--review-requested=@me"],
    ["api", "repos/a/b/pulls/1/comments"], ["api", "-X", "GET", "repos/a/b"], ["api", "graphql", "-f", "query=query { viewer { login } }"],
    ["auth", "status"], ["repo", "view", "a/b"], ["project", "item-list", "1"]]


@pytest.mark.parametrize("args", GH_BLOCKED, ids=" ".join)
def test_gh_writes_are_blocked(guarded, args):
    code, err, reached = guarded["run"]("gh", args)
    assert code != 0 and "blocked" in err and not reached


@pytest.mark.parametrize("args", GH_ALLOWED, ids=" ".join)
def test_gh_reads_pass_through(guarded, args):
    code, _, reached = guarded["run"]("gh", args)
    assert code == 0 and reached


def test_gh_graphql_mutation_in_a_file_is_blocked(guarded):
    q = guarded["tmp"] / "q.graphql"
    q.write_text("mutation { resolveReviewThread(input: {threadId: \"x\"}) { thread { id } } }")
    code, err, reached = guarded["run"]("gh", ["api", "graphql", "-F", f"query=@{q}"])
    assert code != 0 and not reached
    code, err, reached = guarded["run"]("gh", ["api", "graphql", "-F", "query=@/no/such/file"])
    assert code != 0 and not reached, "an unreadable query file counts as a mutation"


def test_gh_graphql_query_in_a_file_is_allowed(guarded):
    q = guarded["tmp"] / "q.graphql"
    q.write_text("query { viewer { login } }")
    code, _, reached = guarded["run"]("gh", ["api", "graphql", "-F", f"query=@{q}"])
    assert code == 0 and reached


def test_git_push_is_blocked_everywhere(guarded):
    for cwd in (guarded["ws"] / "repo", guarded["ws"] / ".local/wt/demo-x", guarded["tmp"]):
        code, err, reached = guarded["run"]("git", ["push", "origin", "x"], cwd=cwd)
        assert code != 0 and not reached


@pytest.mark.parametrize("args", [["checkout", "x"], ["reset", "--hard"], ["stash"], ["commit", "-m", "x"], ["pull"],
                                  ["clean", "-fd"], ["branch", "-D", "x"], ["remote", "set-url", "origin", "x"],
                                  ["config", "core.hooksPath", "/x"]], ids=" ".join)
def test_git_changes_in_the_checkout_are_blocked(guarded, args):
    code, err, reached = guarded["run"]("git", args, cwd=guarded["ws"] / "repo")
    assert code != 0 and "checkout" in err and not reached


def test_git_cannot_reach_the_checkout_another_way(guarded):
    repo = guarded["ws"] / "repo"
    for args, env in ((["-C", str(repo), "checkout", "x"], None),
                      ([f"--git-dir={repo}/.git", f"--work-tree={repo}", "checkout", "x"], None),
                      (["--work-tree", str(repo), "reset", "--hard"], None),
                      (["checkout", "x"], {"GIT_DIR": f"{repo}/.git", "GIT_WORK_TREE": str(repo)})):
        code, err, reached = guarded["run"]("git", args, cwd=guarded["tmp"], env=env)
        assert code != 0 and not reached, args


@pytest.mark.parametrize("args", [["status"], ["fetch", "origin"], ["log", "-1"], ["config", "--get", "user.name"],
                                  ["branch", "--show-current"], ["diff"]], ids=" ".join)
def test_git_reads_in_the_checkout_pass(guarded, args):
    code, _, reached = guarded["run"]("git", args, cwd=guarded["ws"] / "repo")
    assert code == 0 and reached


def test_git_work_in_the_agents_worktrees_passes(guarded):
    code, _, reached = guarded["run"]("git", ["commit", "-m", "x"], cwd=guarded["ws"] / ".local/wt/demo-x")
    assert code == 0 and reached


def test_git_worktrees_only_under_local(guarded):
    repo = guarded["ws"] / "repo"
    code, _, reached = guarded["run"]("git", ["worktree", "add", str(repo / "inside")], cwd=repo)
    assert code != 0 and not reached
    code, _, reached = guarded["run"]("git", ["worktree", "add", str(guarded["ws"] / ".local/wt/new")], cwd=repo)
    assert code == 0 and reached
