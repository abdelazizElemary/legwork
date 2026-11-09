---
name: ticket-work
description: Check or implement a ticket assigned to {{AUTHOR_FIRST}} (GitHub issue or Jira ticket, key like ABC-123) as {{AUTHOR_FIRST}}. "assess" decides, with code proof, whether the ticket has enough context and plans the change per repo. "implement" builds it on fresh branches from the latest default branch, one per repo, tests it, reviews it, commits it and writes the PR manifest for the ticket agent to push as draft PRs. Used by the ticket agent (`ptk`), and whenever the user asks to plan, work on or implement one of their tickets.
argument-hint: "<assess|implement|rework> <KEY-123> <ticket folder> <output file>"
---

# Ticket work

The ticket agent (`~/.claude/ticket-agent`, the `ptk` command) runs this skill headless. It gives you a
ticket folder and an output path. The folder holds:
- `ticket.md`: summary, description, comments, links and attachment names, as the tracker has them
- `attachments/`: screenshots and files (Read the images, they often hold the real bug)
- `context.md`: context {{AUTHOR_FIRST}} added on the dashboard, newest last. It outranks the ticket text.
- `conventions.md`: the workspace, the repos you may change, the branch, commit and PR title formats,
  the base branches and the author identity. Follow it exactly, the agent validates every PR against it.
- `assessment.json`: the last check, when there was one

## Rules that never bend

1. **No assumptions.** Every plan step, file choice and claim about behaviour cites `path:line` on
   `origin/<default branch>`, or the ticket text that says it. Anything you can't prove becomes a
   question, not a guess.
2. **{{AUTHOR_FIRST}} is {{AUTHOR_ROLE}}.** Commits use the repo's configured identity (check it is
   `{{AUTHOR_NAME}} <{{AUTHOR_EMAIL}}>`). {{ATTRIBUTION_RULE}}
3. **New branches only, from the latest default branch.** Create every branch with
   `scripts/ticket_worktree.sh`. Never commit to the default branch or any existing branch. The one
   exception is rework mode: new commits on the branches of this ticket's own open PRs listed in
   `rework.md`, the ones the agent created or a PR of the user's own they linked to the ticket. Never amend, rebase or rewrite a commit that is already pushed. Never touch
   the user's own checkouts (no checkout, pull, reset, stash or edits there). All edits happen inside the
   ticket worktrees under `<workspace>/.local/wt/`.
4. **You never push or open PRs.** The agent does that after its own checks. `git push` and GitHub
   writes are blocked in unattended runs anyway.
5. **The tracker is read-only.** Never comment on, edit, label, assign or close a ticket.
6. **House rules.**
   - Follow the project's reference: find it in `~/.claude/skills/pr-review/references/projects.md` and
     read it in full (its "House architecture" sections, its Verify commands and its "What to check" list).
   - Follow each repo's `AGENTS.md`, `CLAUDE.md` or `CONTRIBUTING.md`, and the project memory index the
     reference names.
   - Code comments, commit messages and PR text follow {{AUTHOR_FIRST}}'s own habits ("How I work" in
     `~/.claude/CLAUDE.md`) and voice (`~/.claude/skills/pr-review/references/voice.md`). The style switches in
     `~/.claude/skills/pr-review/references/policy.md` are checked before anything is pushed.

## Mode: assess

Cheap and fast. No code changes and no test runs. Read code with `git -C <repo> show
origin/<branch>:<path>` and `git -C <repo> grep <term> origin/<branch>`, after
`git -C <repo> fetch --quiet origin <branch>`. The user's checkouts sit on other branches, so don't
read their working tree.

1. Read `ticket.md`, every image in `attachments/`, `context.md` and `conventions.md`.
2. **Find work that already exists.**
   - `ticket.md` lists linked PRs when the tracker knows them. Also run
     `gh search prs <KEY> --owner <org> --json repository,number,state,title,url` (and the issue ref, e.g.
     `owner/repo#12`, for GitHub issues) and `git -C <repo> branch -r --list '*<key lowercase>*'` in every repo.
   - Read the matching PRs (`gh pr view`, `gh pr diff`) to see what they cover.
   - Read the latest comments: QA often reports what still fails after a merge.
3. **Locate the change.** Find which repos and files the fix or feature lives in, with `path:line`.
   - Check the duplicated modules and cross-repo contracts the reference lists.
   - Dead code isn't the place: check the code you'd change is actually reached.
4. **Decide the verdict:**
   - `ready`: the expected behaviour is clear, every change location is proven, and you can say how
     it will be verified (a test, or a repro you can run).
   - `needs_context`: the expected behaviour is unclear, there's no repro for a bug, the description
     is empty, or a product decision is needed.
   - `already_done`: merged or open PRs cover the ask and no later comment says it still fails.
   - `not_implementable`: an epic, a meeting, a review task, docs outside these repos, or work in a
     system you can't reach.
5. **Questions:** at most 5. Each is one plain sentence {{AUTHOR_FIRST}} can answer in a line, starting with
   what you need, e.g. "Should a failed file stop the whole run, or be skipped and listed at the end?"
   Never ask what the code can answer.
6. Write the output file as JSON:

```json
{
  "verdict": "ready | needs_context | already_done | not_implementable",
  "summary": "2-3 plain sentences: what the ticket asks and what the code does today",
  "existing_work": [{"repo": "orders-api", "pr": 354, "state": "merged", "url": "...", "covers": "..."}],
  "repos": [{"repo": "orders-worker", "why": "...", "evidence": ["src/worker/runner.py:674 ..."]}],
  "plan": ["one line per step, each naming the file it touches"],
  "verification": "how it will be proven: the test to add, the repro to run",
  "questions": [],
  "risk": "low | medium | high",
  "risk_reason": "one sentence"
}
```

## Mode: implement

1. **Re-read everything,** including the newest `context.md`. If it is still not safe to implement,
   write the manifest with `"verdict": "needs_context"` and the questions, and stop. Don't write code.
2. **Set up a worktree per repo:**
   `bash ~/.claude/skills/ticket-work/scripts/ticket_worktree.sh <workspace>/<repo> <KEY> <type> <slug>`
   (add `--base <branch>` when `conventions.md` names a base other than origin/HEAD, and
   `--branch-format '<format>'` when `conventions.md` gives one; in a follow-up round also `--round <N>`, and
   `--base <earlier PR's branch>` where `rework.md` says to stack).
   - type is `fix` for bugs and `feat` for features. Use `chore`, `docs`, `refactor` or `test` when
     that's honestly what it is.
   - slug is 2-5 words of kebab-case.
   - It prints the worktree, the branch, `VENV` and `PYTHONPATH`. Use exactly those.
3. **Implement it the house way.** The smallest change that does the job, in the layer the repo uses
   for it.
   - When the change touches a shared package that other repos pin, also bump the pin where the
     reference says pins live, and test the consumers against the new commit.
   - When a module exists in two places, change both copies.
4. **Test it:**
   - A bug fix needs a test that fails on the base and passes on the branch. Run it on both to prove it.
   - New behaviour needs tests in the repo's style (see "Tests" in the reference).
   - Run the reference's Verify commands for each repo: the touched tests, the guard tests it lists,
     lint on changed files, and the typecheck.
   - Compare failures with a base worktree. Only new failures matter, and you must fix those.
     Make the base worktree with `git -C <workspace>/<repo> worktree add --detach <workspace>/.local/wt/<repo>-base-<sha7> <sha>`.
     Worktrees never go inside a repo checkout, and the git guard refuses that.
   - Lock files: regenerate them the normal way (`uv lock`, `npm install`, the repo's export script).
     Don't hand-edit them.
5. **Review it yourself** with the pr-review skill's passes (sections 6 and 7: correctness, tests,
   design, prove every finding), and fix what survives. The PR must pass the review you would give a
   teammate.
6. **Commit** in each worktree with `git -C <wt> commit`. The subject follows `conventions.md`, plus at
   most two short body lines. One commit per repo unless splitting really helps review.
7. **Write the manifest** to the output path:

```json
{
  "verdict": "done | needs_context | blocked",
  "summary": "one or two plain sentences for the dashboard",
  "questions": [],
  "prs": [
    {"repo": "orders-shared", "worktree": "<path>", "branch": "fix/abc-123-x", "base": "main",
     "title": "fix(ABC-123): ...", "body": "...", "order": 1}
  ]
}
```

   - `order` is the merge order: shared packages first, then the repos that pin them.
   - The PR title follows `conventions.md`.
   - The PR body, in {{AUTHOR_FIRST}}'s voice: what changes and why, then what ran to test it (the exact
     commands and their result). When `conventions.md` names the repo's PR template, the body IS that template
     filled in: read it from the base branch, keep every section and its order, replace each `<!-- hint -->`
     with the content (or leave the hint in a section that truly doesn't apply and tick the box that says so),
     tick the boxes that apply, and put the ticket under the related-issues section. The agent refuses a body
     with a missing or empty section or a leftover placeholder. When it pins a shared branch, add one sentence
     saying the pin moves to the merge commit after the shared PR merges. The agent adds the links between the
     PRs, and the ticket link when the template has no place for it.
8. End with one line: `RESULT {"verdict": "...", "prs": <n>}`.

`blocked` means you hit something you can't solve without a person (the build environment, access, a
conflicting open PR). Say what, in `summary`.

## Mode: rework, when it is a follow-up round

`rework.md` says "Follow-up round N". The ticket already has PRs, and the work goes on a **new branch**, never on
an earlier one. The earlier PRs are listed with their state, and "Where each new branch starts" gives the exact
`ticket_worktree.sh` command per repo.
- A repo whose earlier PR is still open: stack on it with `--base <that PR's branch> --round N`, and set the
  manifest's `base` for that PR to the same branch.
- A repo whose earlier PR is merged or closed, or any other repo: no `--base`, only `--round N`.
- Always a new slug. Never commit to, push to or reuse an earlier round's branch or worktree.
- Judge only the new feedback: the new ticket comments and screenshots, and the note at the top of `rework.md`.
  Don't redo what the earlier PRs already did right.
- Then work as in implement mode (steps 3 to 8) and write the manifest the same way. The PR body starts with
  what the earlier PR missed and points at it, e.g. `Follow-up to #123: ...`.
- Answer the earlier PR's comments with `"replies"` as in rework mode (they go on the earlier PR, where the
  comment is). Write `{new_pr}` where the new PR's link belongs; the agent fills it in once it's open.

## Mode: rework

The ticket already has open PRs that the agent opened, and {{AUTHOR_FIRST}} asked for more work on them:
review feedback on the PR, new ticket comments, a scope change, or a ticket that came back. `rework.md` in
the ticket folder lists what to do, the PRs (repo, URL, branch, base, worktree) and the feedback the agent
found. It outranks the ticket text, and `context.md` still applies. When it lists screenshots
({{AUTHOR_FIRST}} added them), Read every one first: they show what was seen, which the text may not say.

1. **Read the request and the feedback in full.** For each PR, also read the conversation yourself:
   `gh pr view <n> --repo <owner/repo> --comments` and `gh api repos/<owner/repo>/pulls/<n>/comments`
   (inline review comments, with `path` and `line`). Read new ticket comments in `ticket.md`. If something
   asked for is unclear or conflicts with the ticket, write the manifest with `"verdict": "needs_context"`
   and the questions, and stop.
2. **Work in the worktrees `rework.md` names, on their branches.** The agent already brought each one up to
   date with its PR branch on GitHub and merged the latest base branch in. Check `git -C <wt> status` is clean
   and `git -C <wt> log -1` is the PR head it lists, or the agent's merge of the base on top of it. When
   `rework.md` says the base conflicts, merge it first as it says and resolve every conflict, then do the
   rework. A worktree marked detached is right as it is: commit there, the agent pushes by name.
   Never switch branches, never make a new branch, never touch other worktrees. When `rework.md` has "Since
   your last push, on GitHub", someone (often the person) already changed the branch, for example resolved
   conflicts: read that first and build on it, don't redo or undo it.
3. **Make the change the house way, test it** (implement steps 3 and 4), and review your own diff
   (implement step 5). Feedback you disagree with: don't apply it, and say why in your reply to that comment,
   with `path:line`.
4. **Commit on top** with `git -C <wt> commit`, subject per `conventions.md`. Never amend or rebase a pushed
   commit, never force anything. One commit per repo is usual.
5. **Write the manifest** like implement mode, listing every PR from `rework.md` with the same repo,
   worktree, branch and base (and its `number`), plus:
   - `"changes"`: one plain line per PR saying what this rework changed, for the dashboard.
   - `"body"`: the PR description again, updated only when what the PR changes changed, else the current one.
     When the repo has a PR template (see `conventions.md`) and the current description doesn't follow it, rewrite
     it into the template: same facts, template sections, checked by the agent.
     The description describes the change. It never answers, quotes or addresses a reviewer: the agent refuses
     a body that does.
   - `"title"`: the current title unless the scope changed.
   - A repo that needs no change keeps its entry with `"changes": ""` and no new commit.
   - `"replies"`: one reply per comment you acted on or decided not to, answering it in place:
     `{"repo": "<repo>", "pr": <number>, "to": "<the reply id rework.md gives>", "body": "<the reply>"}`.
     Say what you changed and where (`path:line`, the commit), or why you didn't change it. Write it in
     {{AUTHOR_FIRST}}'s voice (`references/voice.md`): it's posted under their name. Questions the code can't
     settle (a decision, a root cause you couldn't prove) get an honest reply too, not a fix. Bot reports get
     no reply: fix them in code. The agent posts each reply after the push, in the review thread for inline
     comments and quoting the comment for the others.
6. End with `RESULT {"verdict": "...", "prs": <n>}`. The agent checks the new commits and pushes them to
   the same PRs. When rework.md says the PRs are merged, there is nothing to push to: work like implement
   mode instead, on new branches, as a follow-up PR.

## When the user asks directly (not through the agent)

Do the same work, then tell them to run `ptk finalize <KEY>`, which runs the same checks and opens the
draft PRs. Don't push or open PRs yourself unless they explicitly ask.
