# How the kit fits together

## The parts

| Part | Installed to | What it is |
|---|---|---|
| `pr-review` skill | `~/.claude/skills/pr-review` | The review procedure: worktree, build and test, coverage of changed lines, specialist agents, prove every finding, write a draft, check and post. `references/` holds the user's voice, one reference per project and the index `projects.md` |
| `ticket-work` skill | `~/.claude/skills/ticket-work` | Two modes. `assess` decides if a ticket is ready, with proof, and plans it. `implement` builds it on new branches and writes a PR manifest |
| `agent-lib` | `~/.claude/agent-lib` | Shared by both agents: `runsettings.py` (model and effort), `osglue.py` (notifications, scheduler, login-shell env), `shims/gh` and `shims/git` (guards), `guard.json` (real binaries and protected workspaces) |
| PR review agent | `~/.claude/pr-review-agent`, command `prr` | Polls review requests, runs the skill headless in draft mode, stores drafts, serves the Review desk on 127.0.0.1:8787 |
| Ticket agent | `~/.claude/ticket-agent`, command `ptk` | Polls the tracker, runs `ticket-work assess`, waits for the user, runs `ticket-work implement`, validates and opens draft PRs, serves the Ticket desk on 127.0.0.1:8788 |
| Rules | a marked block in `~/.claude/CLAUDE.md` | The user's standing rules for every Claude session: authorship, voice, no assumptions, review policy, safety |
| Scheduler | launchd plist, systemd user timer or a cron line | Runs `agent.py cycle` every `interval_minutes` |

## The desks' frame

Both desks share a header and a footer. `agent-lib/frame.py` answers `/api/frame` on each desk: the installed kit version
(`agent-lib/kit_version.json`, written by `install.py`) and each desk on the machine, with a link to it when its
`dashboard.json` names a live process. The link carries that desk's token and is only ever sent to the person's own browser
on 127.0.0.1, behind the same token check as the rest of the API.

Both desks share one look, built on Apple's: the system font, Apple's system colours (the accessible variants wherever
text sits on a colour), grouped sheets, capsule buttons, segmented controls and a translucent toolbar. The tokens sit at
the top of each page and the components in an "Apple layer" at the end of its style block, the same text in both desks
(a test compares them). Every text colour must pass 4.5:1 on every surface in light and dark (`tests/test_desk_design.py`),
`prefers-contrast: more` darkens text and lines, and the desks follow the system appearance with no theme switch of
their own. No web fonts are loaded, and the pages' CSP no longer allows them.

Each scheduled cycle starts its desk when it isn't up (`start_desk`), so the desks come back after a restart or a crash.
`dashboard stop` leaves `.dashboard_stopped` behind, and the cycle leaves a desk stopped that way alone until `dashboard`
starts it again. A desk counts as up only when its pid is alive and its port answers (`osglue.desk_up`). Runs a desk starts
are prefixed with `nice -n 10` (`osglue.LOW`), the CPU priority the scheduler's own jobs get from launchd's Background type.

The ticket desk has one scroller, the page. The ticket pane flows with it (no height cap, nothing inside scrolls on its
own), and only the Tell input is pinned, to the bottom of the screen.

## The review flow

1. `prr cycle` (scheduler) runs `gh search prs --review-requested=@me --state=open --owner <org>` for each org.
   Drafts, bots and `skip_authors` are left out.
2. The first cycle ever only records every pending request as `seen` (the baseline). After that, a PR is
   reviewed when it's new or its head commit changed. A failed run is retried up to `max_attempts_per_commit`
   times per commit, and at most `max_reviews_per_cycle` reviews run per cycle.
3. `run_review` takes a per-PR lock and runs
   `claude -p "/pr-review <url> draft ..." --permission-mode auto --permission-prompts none --output-format json --max-budget-usd N`
   in the workspace, with the shims first on PATH, `PR_REVIEW_NO_POST=1`, deny rules for push and posting,
   and allow rules for the skill's own scripts. Model and effort flags come from `runsettings`.
4. The skill writes `<workspace>/.local/reviews/<repo>-pr<N>.json`. Only needed changes are posted: a
   request for changes carries the blocking comments only, and an approve has an empty body and no
   comments. Minor findings go in the run summary under `Not posted:`, which the desk shows under "How it
   was checked" (and `prr show` prints), so they're for the user only. `post_review.py` refuses a draft
   that breaks this. The agent then runs `post_review.py` in
   check-only mode itself, so the checks don't depend on the model, and records the verdict, counts,
   cost, model and session id. A desktop notification says it's ready.
5. The user reads it on the Review desk (or `prr show`) and posts it (`prr post` or the Post button, which
   asks to confirm). `post_review.py --post` re-checks that the PR head hasn't moved, then submits one review.
6. On a re-review, the draft also lists the user's earlier conversations that the new commits fixed
   (`resolve_threads`, each with the `path:line` that shows the fix). The desk shows them before posting.
   Only open threads the user started are accepted, and they're resolved right after the review is
   submitted, never before.

Review states: `seen` (baseline), `running`, `drafted`, `failed`, `posted`, `skipped` (until new commits).

## The ticket flow

```
new -> check_queued -> checking -> ready -> approved (queued) -> implementing -> pr_open
                -> needs_context --(user answers)--^           -> needs_context (stopped to ask)
                -> already_done                                -> needs_attention (PR checks failed)
                -> not_implementable                           -> failed
skipped (any time, by the user)
failed / needs_attention --(Fix it: resume the same session with the fix)--> check_queued or approved
pr_open -> merged | pr_closed       (follows the PRs on GitHub, every cycle)
pr_open | merged | pr_closed --(Rework)--> approved -> implementing -> pr_open (same PRs, or a follow-up PR)
```

1. `ptk cycle` runs the tracker's `search()`: the open tickets assigned to the user. New ones start as `new`
   (types in `skip_types`, e.g. Epic, go straight to `not_implementable`). Tickets no longer returned are
   marked `gone`.
2. With `auto_check: "all"`, up to `checks_per_cycle` new tickets are checked per cycle. Tickets edited in the
   tracker since their last check are checked again, at most once per 6 hours.
3. A check: the tracker's `fetch()` writes the ticket folder (`ticket.md`, `issue.json`, `attachments/`),
   the agent adds `context.md` (the user's notes) and `conventions.md` (branch, commit and title formats,
   repos, bases, author), then runs `/ticket-work assess`. The verdict sets the phase.
4. The user answers questions ("Send and implement" or "Send and check again") or approves. One ticket is
   implemented at a time per free slot (see Parallel runs), in a detached process.
5. `/ticket-work implement` makes worktrees and branches with `ticket_worktree.sh`, implements, tests,
   reviews its own diff, commits, and writes `runs/<KEY>/<run>/manifest.json`. It can't push.
6. `finalize()` validates every PR before pushing anything: the repo is allowed and its origin is the org's,
   the worktree belongs to that repo and is on a new branch named `<type>/<key>-<slug>`, the base is the
   expected default branch, every commit is authored and committed by the user, names the ticket and follows
   the attribution setting, nothing is left uncommitted, no secrets-looking files, the branch doesn't exist on the remote,
   the title matches the configured format, and the body passes the same style lint as reviews. Only then
   does it push and run `gh pr create --draft` in merge order, append the ticket line (`Refs owner/repo#N`
   for GitHub), and cross-link multi-repo PRs with their merge order. `ptk finalize <KEY>` reruns it.

## Safety model

- **No posting by the review agent.** `PR_REVIEW_NO_POST=1` makes `post_review.py --post` refuse, the gh
  shim blocks every GitHub write (`gh api` with a non-GET method or fields, GraphQL mutations, `gh pr review`,
  `comment`, `merge`, `create`, `edit` and more), and the run's settings deny the same commands.
- **No pushing by Claude.** The git shim blocks `git push` for both agents. Only the ticket agent's own
  code pushes, after `validate()` passes, with the real git.
- **The user's checkouts stay untouched.** The git shim refuses mutating git commands (checkout, reset,
  stash, commit and so on) inside the protected workspaces, except under `<workspace>/.local`, and refuses
  `git worktree add` outside `<workspace>/.local`. Edit and Write are denied on the main checkouts in ticket
  runs. Tool caches outside the workspaces (uv, npm) are not affected.
- **The tracker is read-only.** The trackers only GET. The gh shim blocks `gh issue` writes and project
  edits. The ticket token is removed from the Claude run's environment.
- **Budgets.** Every headless run has `--max-budget-usd` and a timeout.
- **Dashboards are local.** Bound to 127.0.0.1, a Host header check (DNS rebinding), a random token kept in
  `.dashboard_token` (0600) passed in the URL for the page and in `X-Token` for the API, JSON-only POSTs, a
  strict CSP and no framing. Ticket and review text is escaped before any markup is rendered.

## Live progress

Every headless run streams its events (`--output-format stream-json --verbose`). `agent-lib/progress.py`
turns each tool step into one plain line ("Reading src/a.ts", "Running the PR's tests", "Started a helper:
code review pass") and keeps the last 60 in `<agent>/progress/<job>.json`, with the run's pid. The desks show
the current step in the list, and the latest steps with times in the open review or ticket. They say when
nothing new happened for 10 minutes (often a long test run), and when the run's process is gone ("stopped"),
so a slow run and a dead one look different. The final result event is the same object `--output-format
json` returns, so costs, session ids and errors are read as before.

## Your profile: voice, habits, review style

Nothing personal ships in the kit. Each person's copy has a git-ignored `profile/` and their own config:
- `profile/voice.md`: how they write review comments, learned at setup from their own comments and PR text,
  with real quotes. It installs as `references/voice.md` (the kit's `voice.md` is only a neutral template).
- `profile/rules.md`: their habits, rendered into the "How I work" part of their CLAUDE.md block.
- `profile/severity.md` (optional): their bar for what blocks a PR, appended to `references/policy.md`.
- `review.policy` and `style` in the config: how an approve looks, whether minor points are posted, and the
  style switches. Install writes them to `references/policy.json` and `policy.md`; `post_review.py` enforces
  them on reviews, and the ticket agent on PR bodies. The kit itself only insists on the attribution setting, real
  `path:line` evidence and suggestions on real diff lines.
- `attribution` in the config: `mode` `"none"` (the default) refuses any tool mention in commits, PR text, reviews
  and replies. `"disclose"` turns that around, and the kit's own code adds the disclosure so it never depends on
  the model: the git guard adds `commit_trailer` to every commit a run makes (`git commit --trailer`, git 2.32+),
  `post_review.py` ends the review body with `footer`, and the ticket agent ends PR descriptions and replies with
  it. The push checks then need the trailer on every commit except merges. Install renders the skills and the
  CLAUDE.md block for the mode, so the model never writes a second one.

Project references (`references/<id>.md`) hold facts about the repos, with `path:line`. `package.py` keeps a copy
in the kit's `references/` folder, which git ignores: your maps describe your own repos and never ship.

## After the PRs open: state, feedback, rework

Every cycle reads each PR with `gh pr view` and its inline comments: draft or ready, review decision, merged
or closed, CI checks, and every comment and review by someone other than the user. Bot reports (CodeQL,
github-actions) are kept apart and don't count as feedback. A comment is "new" when it came after the last
push (`feedback_since`). The ticket's phase follows the PRs: `pr_open`, then `merged` when all merged, or
`pr_closed`. A ticket that comes back after its PRs is flagged (the desk marks it "Came back" and notifies once): one that
leaves the list and returns, or, when `ticket.active_statuses` lists the working statuses, one whose status moves from
any other status into one of them between two polls (Testing to In Progress). Starting the rework clears the flag.

GitHub is the source of truth for a PR's state, so the desk can ask for it at any time, read-only: the
**Refresh PR status** button in the header (every ticket), the **Refresh now** link on a ticket's PR card (that
ticket), `ptk prs [<KEY>]`, and opening a ticket whose PRs were last read more than two minutes ago. A read that
fails keeps the last known state and shows the error. A ticket that is running, or that a click changed while
GitHub was being read, is left as it is.

Rework (`ptk rework <KEY> "..."` or the desk's Rework button) queues an implementation in rework mode:
- While a PR is open, the agent fetches its branch and brings its own worktree (`.local/wt`, detached when
  someone else has the branch checked out) level with it: a suggestion committed on GitHub, a conflict resolved by
  hand, even a force-push when the agent has nothing unpushed. It does this again before a Fix it during a rework,
  and tells the run what changed. It writes `rework.md` (the request, the PRs, all feedback, new ticket
  comments) and runs `/ticket-work rework`. It commits on the same branch. `finalize_rework` checks only the
  rework's own commits, not what a merge of the base branch brought in (author, ticket named, attribution setting, no secrets, clean worktree, title and body rules), refuses
  anything that rewrites a pushed commit or builds on a branch someone moved meanwhile, then pushes
  fast-forward only and edits the title or body when they changed. Nothing is ever force-pushed.
- When every PR is merged or closed, the rework becomes a follow-up PR on new branches.
- With `ticket.rework_branch: "new"` a rework never commits to an existing branch, even while the PR is open. Each
  rework is a follow-up **round** (`round` 2, 3, ...): the earlier PRs move to `previous_prs`, `rework.md` lists them
  with where each new branch starts, and the run makes new branches with `ticket_worktree.sh ... --round N`. A repo
  whose earlier PR is still open is **stacked** on it (`--base <that branch>`, and the PR's base is that branch);
  one whose PR is merged or closed starts from the latest default branch. `validate` accepts a base that is the
  default branch or an open earlier PR branch of this ticket, nothing else, and explains it when the base branch has
  been deleted meanwhile. A failed follow-up is retried as the same round. Before it starts, the agent reads the
  PRs from GitHub, and refuses when it can't, because it can't tell where the new branch starts.
- The PR description follows the repo's own PR template when it has one (`ticket.pr_template`, default `"repo"`;
  `"none"` turns it off). `conventions.md` points the run at it on the default branch. Before pushing, the agent
  checks every template section is there, in order, and filled (text or a ticked box beyond the template's own lines
  and hints; "(if applicable)" sections may stay empty), with no placeholder such as `#issue-number` left. The person's style switches
  then judge only the wording, not the template's headers, bold, checkboxes or length. The `Refs` ticket line is
  added only when the body doesn't link the ticket already.
- Comments are answered where they were left, never in the PR description. `rework.md` gives every comment by a
  person a reply id (`inline-<id>`, `comment-<id>`, `review-<id>`), and the run writes its answers into the manifest's
  `replies`. Before anything is pushed, each reply must target a real comment on one of the ticket's PRs (not a bot's
  report, once) and pass the person's style switches, and a PR body that quotes or @mentions a reviewer is refused.
  After the push the agent posts each reply: inline comments in their review thread, the others as a PR comment that
  quotes the comment. In a follow-up round the reply goes on the earlier PR, with `{new_pr}` filled in. The desk marks
  answered comments "Replied" and lists the replies, and any that failed, under the rework.
- Tell the agent (`/api/tell`, the one input on the ticket pane, pinned to its bottom) queues a read-only `tell` run with its own model,
  effort, slots and cap. The run writes a plan to `tell-<id>.json`; `clean_plan` keeps only the actions in `TELL_ACTIONS`
  with their known fields. `apply_tell` (the Do it button) runs them in order through the desk's own functions
  (`request_rework`, `request_conflict_fix`, park, `sync_ticket_prs` and so on) and stops at the first that's refused. A
  plan runs once; a newer message replaces it. `close` needs `closed_evidence`: a done status in the tracker or every PR
  merged or closed. A desk-closed ticket the tracker reopens comes back (`sync_tracker`). Each new message moves the
  last exchange to `tell_history` (the last 100), and `tell_thread` gives the desk the whole conversation, with exchanges
  from before that read back from the agent log and `tell-<id>.json`. The pane has no other text
  box: answers to the agent's questions (`context`), approving (`approve`), a fix for a failed run (`fix`) and linking a
  PR (`link_pr`) are Tell actions too. A failure whose diagnosis says `needs_you` isn't offered as Fix it again.
- `link_pr` makes an open PR the person opened by hand this ticket's PR: same org and repo, not a fork, opened by the
  signed-in GitHub user, and no other open PR of the ticket in that repo. It goes in `prs` with `linked_at`, and from then
  on it is reworked like the agent's own: its own worktree `<repo>-<key>-pr<n>` (detached when the person's checkout
  has the branch), commits on top, pushed by name, never forced.
- Every rework starts from the latest base branch (`merge_base`, `ticket.rework_merge_base`, default on): after the
  worktree matches GitHub, the agent fetches the base and merges it in as the person. A conflict is undone and written
  into `rework.md` for the run to merge and resolve; `finalize_rework` refuses to push until that base tip is an
  ancestor of the new head.
- A PR that conflicts with its base (GitHub's `mergeable` is `CONFLICTING`, or the merge status is `DIRTY`) gets a
  "Fix the conflict on the same PR" button. The fix is a rework-mode run whose brief says to `git merge --no-edit
  origin/<base>` in the PR's own worktree and resolve it. `finalize_rework` then checks the base really is an ancestor
  of the new head (otherwise the conflict is still there) and pushes fast-forward, like any rework. It never pushes a
  teammate's commit, and nothing is force-pushed. This is the one exception to "never commit to an existing branch":
  only branches the agent itself created, and only for this fix.
- Screenshots can go with a rework: choose files or paste them into the desk's box (`ptk rework <KEY> "..."
  --image FILE`). The desk saves them as `from-you-<time>-<name>` in the ticket's `attachments/` (PNG, JPEG, GIF
  or WebP up to 12 MB, checked by content, never SVG), and `rework.md` lists them so the run reads each one.

The board stays read-only: the desk shows its Status column as it is.

## Pause

Pause stops what spends or writes: checks, implementations, diagnoses, auto-resume. Each cycle still reads the
tracker and the PRs, which is free and read-only, so the desk stays current. A tracker read that fails or
times out is logged and the cycle goes on.

## Parallel runs

Each job (check, implement, diagnose) has a pool of slots: lock files `state/.<job>.slot<N>.lock`, held with
`flock` for the length of a run. The limits come from `run_settings.json` (`parallel`, set on the ticket
desk's Run settings), else `ticket.max_parallel` in the config, else 3 checks, 2 implementations, 3 diagnoses
(at most 8). A ticket that finds no free slot waits in `check_queued` or `approved`. Every finished run and
every cycle calls `start_queued()`, which starts the waiting tickets by sprint (current first) and priority
(P0 first). Implementations never touch each other: each has its own worktrees and branches under
`<workspace>/.local/wt/`. Running several at once uses more CPU and spends more at the same time.

## Failures, diagnosis and fixes

When a check, implementation or PR step fails, `record_failure()` keeps what's needed to explain and fix it:
the result type, timeout, cost against the budget, the session id, the last steps, the last message and the
error output. `explain()` turns that into a plain title and detail at once, with no Claude run: time limit,
budget, a stopped process, a usage limit, sign-in, a model the account can't use, each PR check error.
Then a read-only diagnosis run (job `diagnose`, Sonnet at high effort by default, $2 cap) reads the
transcript, logs and worktrees and writes `diagnosis-<id>.json`: what happened, why, evidence and a fix. The
desk shows both, with the suggested fix in an editable box and three buttons:
- Fix it: resumes the same session (`claude -p --resume <session>`) with the fix, keeping all its work.
- Start over: runs the step from scratch (the instructions go in as a note).
- Run only the PR step: re-runs the push and PR checks when the code needs no change.

A usage-limit failure (Claude's "you've hit your session limit · resets <time>") is not diagnosed. It resumes on its
own once, two minutes after the reset. `ptk fix <KEY> ["instructions"] [--fresh | --pr-step]` does the same
from a terminal.

## Model and effort

Every run button on the ticket desk has its own model and effort picker, and a running check or
implementation shows a picker too. Runs read their prompt from stdin (`--input-format stream-json`), and a
watcher checks the job's chosen model and effort every 3 seconds. When the choice changes (the ticket's own,
or the default it follows), it sends `set_model` and `apply_flag_settings {"effortLevel": ...}` control
requests, so the run switches from its next model call, keeping its context. The review agent does the same.
This protocol is what the Agent SDK uses but isn't publicly documented, so re-test it after a Claude Code
upgrade (tested on 2.1.296 and 2.1.299).


`runsettings.py` resolves, in order: the job's own choice (per PR in `overrides.json`, per ticket in its
state's `run_settings`), the agent's default (`run_settings.json`), then the user's own Claude default from
`~/.claude/settings.json` (no flag passed). Some setups (an API proxy, or a key limited to some models) only allow
some models, so `prr models` and
`ptk models` (or "Check which models work" on either desk) probe the candidates with a tiny prompt and keep
the list of models that answered. Efforts: low, medium, high, xhigh, max.

## Files the agents keep

- Review agent: `state/<org>__<repo>__<n>.json`, `logs/agent.log` and one JSON log per run,
  `overrides.json`, `run_settings.json`, `paused`, `last_cycle`, `dashboard.json`, `.dashboard_token`.
  Drafts live in `<workspace>/.local/reviews/`, worktrees in `<workspace>/.local/wt/`.
- Ticket agent: `state/<KEY>.json`, `tickets/<KEY>/` (the ticket folder), `runs/<KEY>/<run>/` (manifest and
  PR bodies), `logs/`, `tracker_status.json`, `run_settings.json`, and for Jira browser auth
  `.jira_cookies.json` (0600) plus `browser-profile/` and `.venv/`.

## Trackers

`trackers/__init__.py` defines the interface. `github.py` reads GitHub Issues through gh: either the search
API (`is:issue is:open assignee:@me` plus the configured `scope` and `query`), or one Projects board through
GraphQL (open issues assigned to the user, minus the done columns). It downloads images from the issue text
when the gh token can read them, and lists PRs that reference the issue. `jira.py` reads Jira with a PAT
(Data Center), email plus API token (Cloud) or a browser SSO session. Another tracker (Linear, GitLab) is one
new file with the same methods.

## Where to customize

- Review policy and voice: `skills/pr-review/SKILL.md` section 7 and `references/voice.md`.
- Per-project knowledge: `references/<id>.md`.
- Ticket conventions: `ticket.conventions` in the config (title and commit formats), `repos`, `base_branches`.
- Branch types: `TYPES` in the ticket agent and `ticket_worktree.sh`.
- Cadence and budgets: the config.
