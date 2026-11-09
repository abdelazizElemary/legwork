---
name: pr-review
description: Review a GitHub pull request in {{AUTHOR_FIRST}}'s projects (the orgs listed in references/projects.md) end to end and post it as {{AUTHOR_FIRST}}. It builds and tests the PR in an isolated worktree, measures which changed lines tests cover, and checks the change against each repo's own architecture and frontend/backend best practice. It runs specialist passes, proves every finding from the code (no assumptions), comments on the changes that are needed, as GitHub suggestion blocks in {{AUTHOR_FIRST}}'s own voice, and posts only what {{AUTHOR_FIRST}}'s review policy allows. Use whenever the user asks to review, re-review, check or approve a PR, pastes a PR link, or says "review #123" or "look at X's PR" in these projects.
argument-hint: "<pr-url | repo#number | number> [draft] [quick]"
---

# PR review

The review goes out under {{AUTHOR_FIRST}}'s name to teammates. It must be correct, provable, short and easy
to act on. Being wrong in public costs more than missing a nit.

## Rules that never bend

1. **No assumptions. Every comment is proved from the codebase.** Each finding names the `path:line` at
   the PR's head commit that shows the problem, plus the code that proves the consequence (the caller,
   the guard that's missing, the test that fails). Run it when you can: a failing test, a typecheck error
   or a tiny script beats reasoning. If you can't prove it, don't post it. At most, ask it as a plain
   question when the answer changes the verdict. `post_review.py` refuses any comment without
   `evidence` holding a `path:line`.
2. **{{AUTHOR_FIRST}} is {{AUTHOR_ROLE}}.** {{ATTRIBUTION_RULE}}
3. **Don't touch anyone's branch or checkout.** Never push to the PR branch, the default branch or any
   existing branch. Never checkout, pull, reset, restore or stash in the user's own checkouts. All local
   work happens in the review worktree from `scripts/pr_worktree.sh`.
4. **Subagents are read-only.** Every agent prompt includes the read-only clause below. Never run the
   `code-simplifier` agent during a review, because it edits files.
5. **Verdict:** REQUEST_CHANGES only when at least one proven blocking issue exists (section 7 lists
   them). That list includes untested critical backend code and clear violations of the repo's own
   architecture. Otherwise APPROVE, the way `references/policy.md` says.
   On {{AUTHOR_FIRST}}'s own PR, GitHub only allows COMMENT.
6. **The requested change is code.** Each comment carries a `suggestion` block the owner can commit, or
   a short code block when a suggestion can't express it.
7. **What's posted follows `references/policy.md`.** Every blocking issue gets a comment. Minor findings
   are posted or kept in the terminal summary under `Not posted:` as the policy says, so {{AUTHOR_FIRST}} can
   decide. The case and then the ask, in `references/voice.md`. Suggestion mechanics: `references/suggestions.md`.

## Arguments

- Target: a PR URL, `repo#N`, or `N` (the repo comes from the cwd). Resolve it to org, repo and number.
- `draft`: do everything except post. Show the review and the draft file path.
- `quick`: typecheck, lint and the PR's own tests only, with no full suites and no specialist agents.
  Use it for small PRs or when asked. Still check that critical backend changes have a test (grep plus
  coverage from the PR's own tests), and still do the design pass.

## Unattended runs

The PR review agent (`~/.claude/pr-review-agent`, the `prr` command) runs this skill headless with
`draft` whenever {{AUTHOR_FIRST}} is requested as a reviewer. When the prompt says the run is unattended:
- never ask questions and never try to post (GitHub writes and `git push` are blocked)
- always write the draft to the path given, even for a clean approve
- when a check can't run, say so in the summary and don't claim it in the body
- end with the `RESULT {...}` line the prompt asks for

## 1. Identify the project and load context

- Read the remote with `git -C <repo> remote get-url origin`, or parse the URL.
- Open `references/projects.md`. It maps each org to its reference file (`references/<project>.md`), its
  workspace folder and its project memory index. Read that reference in full. If the org isn't listed,
  say so and review with the generic rules only, labelling house-pattern claims as unproven.
- Read the project memory index the reference names, if any. Settled decisions there are not findings.
- Always read `references/voice.md` and `references/policy.md` before writing anything.
- `export GH_HOST=<host from the remote>` ({{GH_HOST}} by default), and pass `--repo <org>/<repo>` to every `gh pr` command.

## 2. Read the PR

```bash
gh pr view N --repo ORG/REPO --json number,title,url,author,state,isDraft,baseRefName,headRefName,headRefOid,body,additions,deletions,changedFiles,commits,files,reviewDecision,mergeable,statusCheckRollup
gh api user --jq .login
gh api repos/ORG/REPO/pulls/N/reviews --jq '.[] | {user: .user.login, state, commit_id, body}'
gh api repos/ORG/REPO/pulls/N/comments --paginate --jq '.[] | {id, user: .user.login, path, line, in_reply_to_id, body}'
gh api repos/ORG/REPO/issues/N/comments --jq '.[] | {user: .user.login, body}'
```

- If it's closed or merged, stop and say so. If it's a draft, review it but say it's a draft.
- If {{AUTHOR_FIRST}} already reviewed it, use re-review mode (section 10).
- Don't repeat what a teammate already said in a comment. Agree in one line only if it matters for the verdict.
- Get the intent from the title, body and ticket id (the reference names the ticket format). Review
  against what the PR claims to do, plus anything it breaks.
- Check overlapping open PRs with `gh pr list --repo ORG/REPO --state open --json number,title,author,files`.
  Another PR touching the same files is a heads-up for the body, not a finding.

## 3. Build and test it locally

```bash
bash ~/.claude/skills/pr-review/scripts/pr_worktree.sh <local repo dir> N --with-base
```

It fetches the PR into `refs/remotes/origin/pr/N`. It creates a detached worktree at
`<workspace>/.local/wt/<repo>-prN`, plus a base worktree at the merge base. It sets up deps and prints
`WORKTREE`, `HEAD`, `BASE`, `AHEAD`/`BEHIND`, `MERGE_WITH_BASE`, `DEPS`, and for Python `VENV` and `PYTHONPATH`.
Keep `HEAD`, since it goes into the draft as `reviewed_head`.

If the repo isn't cloned locally, clone it to `<workspace>/.local/clones/<repo>` first (never inside
another repo), then run the script on that clone.

Run the project's checks from the reference (typecheck, lint, the PR's tests, then the wider suite
unless `quick`). Run the same checks on the base worktree, or compare against a known baseline. Only
failures that are new on the PR count, and a new failure is blocking. If `MERGE_WITH_BASE` reports
conflicts, or the base moved ahead in the same area, check the merged result too.

Long suites go in the background (`run_in_background`) while you read the code.

**Measure test coverage of the change.** Do this always for backend repos, and for frontends that have
a test runner. Produce an lcov report from the worktree (the command is in the reference) and run:

```bash
python3 ~/.claude/skills/pr-review/scripts/diff_coverage.py --worktree <WORKTREE> --base <MERGE_BASE> --lcov <lcov.info>
```

It lists, per changed file, the changed lines no test runs. Use the full unit suite's coverage when
you can, because a partial run makes other modules look untested. Before calling code untested, also
check `git -C <wt> grep -l '<module or symbol>' -- '<test globs>'` finds no test for it. The output
goes in `evidence`, e.g. `diff_coverage: src/services/review.service.ts 0/14 changed lines run, 88-101`.

## 4. Understand the change

- Read every changed file in full at head, not only the hunks: `git -C <wt> diff <MERGE_BASE> HEAD`.
- For each changed function, signature, response shape, event, env var or DB column, find its callers
  and consumers with `git -C <wt> grep`. For contracts, grep the sibling repos too (they're read-only
  reference, never modify them).
- Check the duplicate-code and cross-service lists in the reference. A fix that lands in one copy only
  is a finding.
- When behaviour is unclear, run it: write a scratch test or script under `/tmp`, not in the repo.

## 5. Specialist passes (skip with `quick` or for PRs under ~150 changed lines)

Launch the ones that apply in parallel, in one message, with the Agent tool:

| Agent (`subagent_type`) | When |
|---|---|
| `pr-review-toolkit:code-reviewer` | always: bugs, logic, project rules |
| `pr-review-toolkit:silent-failure-hunter` | try/catch, fallbacks, retries, default values, swallowed errors |
| `pr-review-toolkit:pr-test-analyzer` | always for backend changes, and frontends with tests. Give it the `diff_coverage.py` output |
| `pr-review-toolkit:type-design-analyzer` | new types, DTOs, ORM or pydantic models, schemas |
| `general-purpose` with a design brief | new modules, layers, endpoints, components or hooks. Brief: the reference's "House architecture" section, and find the 2+ existing examples for every pattern it cites |
| `general-purpose` with a security brief | auth, tokens, roles, uploads, paths and keys, raw SQL, logging of user data, tenant filters |

For React frontends, also go through the `vercel:react-best-practices` checklist when it's installed
(hooks, effects, accessibility, rendering, TypeScript). Only what passes the proof bar in section 6 gets posted.

If the toolkit agents aren't available, use `feature-dev:code-reviewer` and `general-purpose`. For a
very large PR (40+ files), split the code-reviewer pass by area. Use the Workflow tool only when
the user asked for a workflow.

Prompt template (fill in every field):

```
Review PR <url> "<title>" read-only. Worktree: <WORKTREE> at <HEAD>; diff with `git -C <WORKTREE> diff <MERGE_BASE> HEAD`.
Intent: <one line from the PR body/ticket>. Focus: <aspect>.
Project context: <the 3-6 lines of the reference that matter for this aspect>.
READ-ONLY: do not edit, create or delete files; do not run git checkout, switch, pull, reset, restore, stash,
commit or push; do not touch any other checkout. Reading sibling repos and running read-only commands, tests or
typechecks in the worktree is fine.
Report only problems this PR introduces that you can prove from the code. For each: path:line at HEAD, the exact
input or state that triggers it, what goes wrong, the proof (path:line of the code that shows it, or the command
you ran and its output), and the smallest fix as exact replacement lines. Say "none" if nothing survives.
No style nits, no formatting, no comment-style remarks, no "consider", nothing a linter catches.
```

## 6. Your own pass: correctness, tests, design

Go through the reference's "What to check" list against this diff, and add anything the agents missed.
Then do two more passes.

**Tests.** Check each piece of changed logic against the coverage output:
- Is there a test that runs it, and does that test assert the behaviour (not only that it ran)?
- Does a bug fix come with a test that fails on the base worktree and passes on the PR? Run it on both to prove it.
- Do the mocks follow the repo's test conventions (see "Tests" in the reference), and do they leave the code under test real?
- When a test is missing, write it in the repo's style in the review worktree, run it and see it pass.
  For a bug fix, also see it fail on base. Then `git -C <wt> restore .` (delete the new file if it's
  untracked). The written test becomes the requested change.

**Design and best practice.** Hold the change against the reference's "House architecture" section:
the repo's own layering, helpers, UI kit, data access, error handling and naming. A design finding
counts only when one of these holds:
- **It breaks a proven house pattern.** Cite at least two existing places that follow the pattern
  (`path:line`), check the repo doesn't mostly do it the other way, and cite the PR line that deviates.
  A pattern that one repo follows and another doesn't is not a rule for the other repo.
- **It causes a concrete defect you can show.** Examples: an N+1 query in a loop, a missing effect
  cleanup that leaks a subscription, a stale closure, a shared helper re-implemented with different
  behaviour, the same fix landing in only one of two copies.

Generic textbook advice ("use a factory", "split this class", "add an interface") with neither of
those behind it is not posted.

## 7. Prove every finding (the step that matters most)

Collect the candidates from the agents and your own pass, then for each one:

1. Open the code at head in the worktree and confirm the exact line and claim.
2. Check the PR introduced it, with `git -C <wt> diff <MERGE_BASE> HEAD -- <path>` and `git blame`. If it
   was already there, drop it, unless it's serious, in which case it gets one `Not posted:` line in the summary.
3. Make it concrete: the input or state that triggers it. Prove it with a test, a script or the
   typecheck when possible. Use the read path (callers, validation, middleware, DB constraints) to check
   it can actually happen. If something else already handles it, drop it.
4. Check it isn't a settled decision (project memory, PR body, ticket).
5. Check a senior teammate would want it raised. Otherwise drop it.
6. Write the fix, apply it in the worktree, and run the typecheck and the touched tests. Then
   `git -C <wt> restore .`. If the fix doesn't hold, rework it or drop the comment.
7. Record the proof in `evidence` (path:line plus what you ran). Anything you can't fill in honestly is not posted.

When there are many candidates, verify them in parallel with read-only `general-purpose` agents told to
"try to prove this finding false", and keep only the ones that survive.

Blocking means one of these. A `Severity` section in `references/policy.md` (the reviewer's own bar) or in the
project reference overrides this list where they differ, the policy's first:
- the build, typecheck or tests fail and didn't fail on base (when a repo has no CI, this is the only
  gate), or they fail once merged with the latest base
- a crash or exception on a normal path
- a wrong result or regression in the main flow the PR touches
- data loss or corruption, a missing or broken migration, a schema and client mismatch
- security: authz bypass, a tenant or ownership filter that's missing, injection, path traversal, SSRF,
  secrets in code or logs
- a contract change (response shape, event, shared dependency pin) without the matching consumer change
- a project rule enforced by tests or CI (the reference lists them)
- **untested critical backend code**, proven with `diff_coverage.py` and a grep for tests. Critical
  means:
  - business decisions (approve, reject, return, status changes, eligibility, scoring, pricing)
  - auth or permission checks, and tenant or ownership filters
  - data writes (create, update, delete, transactions, cross-service writes)
  - migrations and data transforms
  - a bug fix with no test that fails on base
- **a clear violation of the repo's proven architecture that will cost later**:
  - DB or ORM access outside the layer the repo uses for it
  - business logic in routes or controllers when the repo keeps it in services
  - frontend API calls that bypass the repo's API layer
  - hand-built or restyled UI where the repo's UI kit has the component
  - re-implementing an existing helper instead of reusing it
  - a cross-service write of a fact another service owns
  - a fix that lands in only one of two copies
  - a new module that ignores the repo's stated module shape (the reference names it)

Everything else that survived is minor. That includes untested non-critical code (small helpers,
mapping, formatting) and smaller design improvements. Minor findings go where `references/policy.md` says. Any
that aren't posted go in the terminal summary under `Not posted:`, one line each with the `path:line`. A serious problem that isn't
from this PR goes there too. Drop the rest.

## 8. Decide

- Any blocking issue: `REQUEST_CHANGES`, with the blocking comments only and the body `references/voice.md` asks for.
- Otherwise: `APPROVE`, as `references/policy.md` says (by default an empty body and no inline comments).
- {{AUTHOR_FIRST}}'s own PR: `COMMENT`, and don't post unless asked. Show the findings in the terminal.
- When a build or test can't be run (missing registry access, no DB), say so plainly in your reply
  and in the summary. Never write "tested locally" for checks you didn't run.

## 9. Write, check and post

Write the draft to `<workspace>/.local/reviews/<repo>-pr<N>.json` (the shape is in the
`scripts/post_review.py` docstring). Each comment has `path`, `line` (and `start_line` for a range),
`body`, `blocking`, `evidence`, and `replaces` when it has a suggestion. On a re-review, `resolve_threads`
lists the earlier conversations the new commits fixed (section 10). The body and comments follow
`references/voice.md` exactly.

```bash
python3 ~/.claude/skills/pr-review/scripts/post_review.py <draft.json>          # check and preview
python3 ~/.claude/skills/pr-review/scripts/post_review.py <draft.json> --post   # submit as one review
```

The check confirms each comment sits on a diff line and each suggestion replaces the lines named in
`replaces`. It also checks the event matches the blocking flags and `references/policy.md` (what an
approve may carry, whether minor comments are allowed), every thread to resolve is still open, was started by
{{AUTHOR_FIRST}} and has proof, the PR hasn't moved since `reviewed_head`, and the text follows the attribution setting and
passes {{AUTHOR_FIRST}}'s style switches from the policy. Fix every
ERROR, and fix warnings unless there's a clear reason. If the PR moved, review the new commits before posting.

Post unless `draft` was given. The review goes out as one submission, so the owner gets one notification.

Then reply in the terminal, short:
- the verdict and the review link
- what ran and the result (e.g. "tsc, eslint, build pass. jest 53/53. Merged with main: clean.")
- test coverage of the change (e.g. "changed lines run by tests: 16/22, untested: option.controller.ts 61-65")
- the design pass result, in one line
- one line per posted comment (`path:line`, the point)
- on a re-review, one line per conversation it resolves (`path:line`, what fixed it), and the ones left open and why
- `Not posted:` one line per minor finding (`path:line`, the point), or nothing when there are none
- anything not checked, and why

## 10. Re-review mode

When {{AUTHOR_FIRST}} already reviewed the PR:
- Find the last reviewed commit (`commit_id` of the latest review by the viewer) and review
  `git -C <wt> diff <that commit> HEAD`, plus whatever those changes touch. If the commit is gone (a
  force-push), do a full review.
- List {{AUTHOR_FIRST}}'s open threads:
  ```bash
  gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved path line comments(first:20){nodes{author{login} body}}}}}}}' -F o=ORG -F r=REPO -F n=N
  ```
  For each one, check in the code whether it was fixed (prove it like a finding).
- **Resolve what's fixed.** Put every unresolved thread that {{AUTHOR_FIRST}} started and the new commits fixed
  into `resolve_threads` in the draft: the thread `id`, its `path` and `line`, a one-line `comment` (what
  {{AUTHOR_FIRST}} asked), and `evidence` with the `path:line` at head that shows the fix. `post_review.py --post`
  resolves them right after the review is submitted, so nothing is resolved before {{AUTHOR_FIRST}} posts.
  Leave a thread open when the fix is partial, when the owner pushed back without changing the code, or
  when someone else started it. When unsure, leave it open.
- If every earlier blocking item is fixed and nothing new is blocking: APPROVE as `references/policy.md` says.
- When {{AUTHOR_FIRST}} agrees an item can wait, {{AUTHOR_FIRST}} can reply on the thread, e.g. `Leaving this for a
  follow-up: <reason>.` Only when asked. Post replies with
  `gh api repos/ORG/REPO/pulls/N/comments/<comment_id>/replies -f body='...'`.

## 11. Pushing a fix for the owner (only when explicitly asked)

- Never push to their branch. Create a new branch in the review worktree from the PR head:
  `git -C <wt> switch -c <ticket>-review-fixes`. Commit there and open a PR whose base is their branch
  (`gh pr create --repo ORG/REPO --base <their branch> --head <new branch>`).
- First check `git -C <wt> config user.name` and `user.email` show {{AUTHOR_NAME}}
  <{{AUTHOR_EMAIL}}>. No trailers.
- Commit message, PR body and code comments the way {{AUTHOR_FIRST}} writes them (`~/.claude/CLAUDE.md`, "How I
  work", and `references/voice.md`), with the ticket id when the reference asks for one. No footer.
- Push with `git push -u origin <new branch>`. If the reference says force push is blocked, a stale
  branch gets the base merged in, never rebased.
