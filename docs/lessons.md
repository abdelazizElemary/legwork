# Lessons so far

What broke while building and running these agents, and what the kit does about it. Check these first
when something fails.

## Running headless under a scheduler

- **"Not logged in" in scheduled runs only.** launchd and systemd don't read `~/.zshrc` or `~/.bashrc`, so
  gateway credentials exported there are missing. The agents read the vars named in `shell_env` from an
  interactive login shell when they're not in the environment (`osglue.claude_env`). Put the real var names
  in `shell_env`.
- **An LLM gateway may only allow some models.** A 403 like "team not allowed to access model" means that model
  is off for this account. `prr models` probes which ones answer. The doctor's health check passes no
  `--model` on purpose.
- **Auto mode can block the skill's own scripts in unattended runs.** The agents pass allow rules for
  `bash .../pr_worktree.sh`, `python3 .../diff_coverage.py` and `python3 .../post_review.py`, and the review
  agent runs the posting checks itself after the run instead of trusting the model to.
- **PATH is minimal under a scheduler.** `run_path` must hold node, npm, uv, jq and whatever the repos'
  checks need. The doctor lists what's missing. Re-run install (or edit `path` in the agent config) after
  installing a new tool.
- **`--effort default` isn't a valid flag.** "default" means "pass no flag" everywhere in the kit.

## Git and worktrees

- **Never touch the user's checkouts.** Every review and ticket runs in `<workspace>/.local/wt/`. The git
  shim enforces it.
- **The git shim was too broad once.** It blocked `git reset` that uv runs inside its own cache. It now
  protects only the configured workspaces, except `<workspace>/.local`.
- **A run made a worktree inside a checkout once.** The shim now refuses `git worktree add` outside
  `<workspace>/.local`, and the skills say where base worktrees go.
- **Some repos gitignore `package-lock.json`.** `npm ci` fails there. The worktree scripts compare the
  declared dependencies with jq and fall back to `npm install`. They link the main checkout's
  `node_modules` when the dependencies match, and copy the Prisma client when the schema differs, so
  `prisma generate` can't rewrite the user's copy.
- **Lock files must be regenerated, never hand-edited** (`uv lock`, `npm install`).
- **origin/HEAD unset.** `ticket_worktree.sh` stops and says so. Set it with
  `git remote set-head origin --auto` (with the user's OK) or set `base_branches` in the config.
- **Python repos that pin a sibling repo by commit** fail imports when the venv has an older editable
  install. Add `<workspace>/.local/hooks/pythonpath.sh` so the worktree scripts put the pinned commit first.

## The ticket agent

- **A crash in the PR step left a ticket stuck on "implementing".** The state wasn't saved before
  `finalize()` reloaded it, and `sys.exit` skipped the error handling. Now the state is saved first,
  `finalize()` raises a normal error, and every cycle marks runs whose process died as failed
  (`heal_stopped_runs`).
- **A re-check loop kept spending.** The tracker's search returned `updated` in whole seconds and the
  issue API in milliseconds, so every checked ticket looked edited on every cycle. Timestamps are now
  compared to the second, and an automatic re-check waits at least 6 hours after the last check. When adding
  a tracker, check the two endpoints agree on the timestamp format.
- **A run saving its older copy erased the user's notes.** The state has a `user_rev` counter. `save()`
  keeps the user's newer `context` and `run_settings` when the copy being saved is older.
- **Draft PRs, read-only tracker by default.** The PR body gets a `Refs owner/repo#N` line for GitHub issues
  (configurable in `issue_line`; `Closes {ref}` would close the issue on merge).
- **Private issue images.** GitHub's private user-attachments may refuse the gh token. Those images stay
  links on the desk, and `ticket.md` says they couldn't be fetched.

## The dashboards

- **Re-rendering on every poll** reset scroll positions, open sections and focus. Lists re-render only when
  their data signature changes, open sections are kept in localStorage, and the pane keeps its scroll and
  typed text.
- **`[hidden]` lost to `display: flex`.** Both pages have `[hidden] { display: none !important; }`.
- **The folding arrow drifted when open.** A rotated text glyph sat off-centre. Both pages use one CSS
  chevron centred on the summary's first line that turns in place.
- **"Reviewing now" for 20 minutes said nothing.** You couldn't tell a slow run from a stuck one. Runs now
  stream their steps and the desks show the current one, the time since the last one, and "stopped" when
  the process is gone (`agent-lib/progress.py`).
- **A click during the first load was overridden** by the "reopen the last ticket" step. It only reopens
  when the user hasn't picked one in the meantime.

## Running many at once (kit 1.4)

- **One slot made tickets wait for nothing.** With a single global implementation lock, an approved ticket
  waited for another one to finish even when the machine had room. Slots per job now let several run at once.
- **A PR step that can't pass was retried again and again.** A style failure in the PR body fails every time,
  because re-running the PR step doesn't change the body. The explanation now says so, and Fix it resumes the
  implementation session to rewrite the body before the PR step runs again.
- **A usage limit looked like a bug.** A "session limit" message from Claude ended a run within seconds. It's explained as a usage limit, gets no diagnosis (that would hit the same limit) and resumes on
  its own after the reset.
- **Each agent keeps its own list of models.** `prr models` doesn't fill the ticket agent's list. Run
  `ptk models` too, or the desk's pickers only offer the defaults.
- **The live switch needs the prompt on stdin.** With `--input-format stream-json` the process waits for more
  input after the result, so the runner closes stdin when the result arrives.

## After the PRs open (kit 1.5)

- **Pause froze the desk.** A paused cycle returned before reading anything, so the board status
  and the PRs never refreshed. Reading now goes on while paused; pause only stops runs.
- **"Draft" was a label, not a state.** The desk said draft from the config, and never read the PR again after
  opening it. The PRs had been marked ready for review on GitHub. Each cycle now reads every PR.
- **A gh call that hangs crashed the cycle.** A 120-second GraphQL timeout raised out of the cycle. Tracker
  errors are now caught and logged, and the PR sync still runs.
- **Bot comments aren't feedback.** A code-scanning bot (CodeQL, for example) may comment on every PR. Those are shown as bot reports and
  passed to a rework, but don't count as new comments to act on.
- **Rework on the same PR stays narrow.** It only adds commits to the branch the ticket agent created for that
  ticket's open PR, and it never force-pushes.

## Guards need tests (kit 1.8)

- **The guards had gaps nobody had tried.** `gh api -XPOST` (method stuck to the flag) looked like a read, a
  GraphQL mutation in a `-F query=@file` wasn't seen, and `git --git-dir/--work-tree` or `GIT_DIR` reached a
  person's checkout past the "only `-C`" check. Each one is now a test in `tests/test_shims.py`, and every new
  way to write gets a guard and a test before it ships.

## Replies (kit 1.11)

- **A rework answered the reviewer in the PR description.** The run can't write to GitHub (the guard blocks it on
  purpose) and the manifest had no place for replies, so the description was the only outlet, and the summary asked
  the user to reply by hand. Replies are now part of the manifest, checked before the push and posted in place by
  the agent's own code after it.

## PR templates (kit 1.12)

- **A repo's PR template clashed with strict style switches.** A rule for plain PR summaries, or switches such as no
  headers, no bold or a word limit, would refuse a filled-in template. The repo's template is now the description's
  shape, checked section by section, and the style switches judge only the wording.

## Starting from GitHub (kit 1.13)

- **"Conflicts resolved, check the latest" kept failing.** A Fix it resumed the old run without reading the PR
  branch again, so the run never saw the resolution. The branch had also been moved to a worktree outside `.local`,
  where the git guard (rightly) blocks commits, and the PR step counted the teammates' commits a merge of the base
  brought in as the agent's own. A rework and a fix now read GitHub first, adopt a branch rewritten by hand, always
  work in the agent's own worktree, and check only the agent's commits.

## Desks after a restart (kit 1.17)

- **"The agents aren't live" after the laptop was restarted.** The scheduler jobs came back, the desks didn't: only
  `ptk dashboard` started them, and a `dashboard.json` from before the restart could name a pid that another process
  now has. The cycle starts the desk now, and up means the port answers.
- **The ticket pane couldn't be scrolled.** The Tell plan sat in the pane's head, which doesn't scroll, and grew past
  the pane's height, so the scrolling body ended up below the clipped edge. Measure the real layout in a browser
  before guessing.

## Your own PR (kit 1.18)

- **A rework on a PR opened by hand looped.** The person asked to continue on their own PR, the run did the work, and
  the agent refused to push to a branch it hadn't created. Each "Fix it" or Tell rework ran into the same wall, and the
  only way out was a manual push. PRs can be linked now, and a failure that needs the person isn't offered as a rerun.
- **Five text boxes were too many.** Rework, fix, answer, note and Tell each had one, and it wasn't clear which did
  what. Tell does all of it now, from one box at the bottom.

## One scroll (kit 1.18.1)

- **Scroll areas inside scroll areas.** The pane scrolled inside the page, the Tell exchange inside the pane, and the
  input and error output inside those. The wheel did something different depending on where the pointer was. The page
  is the only scroller now, and the one thing that stays put is the input.

## A design review (kit 1.19)

- **Muted text failed contrast in 45 places.** The secondary gray measured 2.8 to 3.4:1 on its surfaces. Every text
  colour is now checked against every surface in a test, light and dark, so a palette change can't bring it back.
- **The counts you click were 9px wide.** A link is the whole phrase now.
- **"Do it" hid what it does.** A plan's button names what it starts and what it can cost.

## Going public (2.0)

- **A project map describes someone's private repos.** The kit folder held mapped references so a reinstall
  started from them, and anything in that folder can be committed by mistake. `references/` is git-ignored now,
  and a test fails if a map is ever tracked.
- **Hiding attribution isn't everyone's rule.** Some employers and projects require AI disclosure. It's a setting
  now, and the kit's own code adds the trailer and footer, so it doesn't depend on the model remembering.

## Costs

- Cost grows with the size of the PR or ticket and the model and effort: large multi-file PRs and long test
  runs cost the most. Every run shows its cost on the desk, and each ticket adds up its own.
- Keep the budgets in the config as caps, not targets.
