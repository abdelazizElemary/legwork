# Mapping a project into its reference

The review and ticket skills are only as good as `references/<project>.md`. It tells them how to build and
test each repo, which house patterns are real, and what counts as a blocking rule. Build it once per project
and refresh it when the architecture moves. Do it read-only.

## Rules

- Read-only. Use `git -C <repo> fetch --quiet origin <default>`, then read with
  `git -C <repo> show origin/<default>:<path>` and `git -C <repo> grep <term> origin/<default>`. The user's
  checkouts may sit on other branches, so don't read their working tree as the truth, and never checkout,
  pull, reset or stash in them.
- Every pattern you write down cites at least two `path:line` places that follow it, checked on the
  default branch, and you check the repo doesn't mostly do it the other way. One example is an anecdote,
  not a house rule.
- Every Verify command is one you ran from a worktree and saw work (make one with `pr_worktree.sh` on any
  recent PR, or `git worktree add --detach <workspace>/.local/wt/<repo>-map origin/<default>`).
  Record the known failures on the default branch, so later reviews only count new ones.
- Keep it tight: about 150 to 300 lines per project. It's loaded into every review.

## Steps

1. List the repos in the workspace and their default branches, languages and frameworks (package.json,
   pyproject.toml, go.mod, pom.xml, Dockerfiles, CI files).
2. Read the repo docs that agents and people already follow: README, CONTRIBUTING, AGENTS.md, CLAUDE.md,
   ADRs. Note rules that tests or CI enforce, and find the test that enforces each.
3. Map how a request flows through each backend (entry, auth, validation, service, data access, events) and
   write each layer as a pattern with two citations.
4. Map the frontend: API layer, UI kit, state, routing, forms, error handling, tests.
5. Find code that exists twice (same module in two repos, copy-pasted helpers), and contracts between repos
   (shared packages, pinned versions, events, response shapes).
6. Work out the Verify commands per stack: tests, lint, typecheck, build, coverage to lcov. Run them.
   Python repos often need an env var or two to skip real config; record them. If repos pin a sibling repo
   by commit, write `<workspace>/.local/hooks/pythonpath.sh` (it gets the worktree path and prints extra
   PYTHONPATH entries) so the worktree scripts pick it up.
7. Fill `references/<project>.md` from `_template.md`, then check it against two or three recent merged PRs:
   would the reference have told a reviewer what mattered in them?
8. If there's a project memory folder for this workspace, put the test baselines and traps there too, and
   name its index in the reference.
