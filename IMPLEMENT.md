# IMPLEMENT: the runbook for the Claude session doing the setup

You are setting up two unattended agents for the user on this machine, from this kit. Work through the
steps in order. Each step says what to check before moving on. `kit.json` lists every file, where it
installs and what every config field means. `docs/design.md` explains the moving parts.
`docs/lessons.md` lists what went wrong the first time; read it before step 4.

## 0. Ground rules for this session

- Ask before anything that costs money (a headless review, a ticket check, `prr models`), writes to GitHub,
  or starts a scheduler job.
- Never checkout, pull, reset, restore or stash in the user's checkouts. Read code with
  `git -C <repo> show origin/<branch>:<path>` and `git -C <repo> grep <term> origin/<branch>` after a fetch,
  or make a worktree under `<workspace>/.local/wt/`.
- The user is the only author. Nothing you write (config, commits, PR text, reference files) mentions
  Claude, AI or an agent as an author. No `Co-Authored-By`, no "Generated with" lines.
- No assumptions. Every fact you put in a reference file has a `path:line` or a command you ran.
- Keep the user's `kit.config.json` and `profile/` out of public places: they hold the user's working rules.

## 1. Preflight

Run these and note the results. Fix or report anything missing before going on.

```bash
uname -a; python3 --version          # Python 3.9 or newer
claude --version; claude -p "Reply with exactly: ok" --output-format json --max-budget-usd 1
gh --version; gh auth status         # note the host(s) and login
git --version; git config --global user.name; git config --global user.email
which jq node npm uv uvx             # jq is needed; the others depend on the repos
```

- If `claude -p` fails with "Not logged in" here, the credentials may come from env vars set in the shell
  rc file. Note their names (e.g. `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`) for `shell_env` in step 3:
  launchd and systemd don't read shell rc files, so the agents read those vars from a login shell.
- Check git over SSH works for the GitHub host: `git ls-remote <any repo remote> HEAD`.
- Check the plugins the review skill uses: `pr-review-toolkit` (its code-reviewer, silent-failure-hunter,
  pr-test-analyzer and type-design-analyzer agents) and `feature-dev` as the fallback, both from the
  `claude-plugins-official` marketplace. Look for them in `~/.claude/settings.json` under `enabledPlugins`.
  If they're missing, ask the user to run `/plugin install pr-review-toolkit@claude-plugins-official` (and
  `feature-dev@claude-plugins-official`) in Claude Code. The review still works without them, with
  `general-purpose` agents.

## 2. Find the facts

Detect, don't ask, wherever you can:

| Field | How to find it |
|---|---|
| author name and email | `git config --global user.name/email`, or the identity on the user's recent commits in these repos |
| github_host | the host in the repo remotes (`git -C <repo> remote get-url origin`), `gh auth status` |
| projects | folders that hold the user's repo checkouts. Group repos by org (from the remote). One project per org: `id` (kebab-case), `org`, `label` (display name), `workspace` (the folder) |
| default branches | `git -C <repo> symbolic-ref refs/remotes/origin/HEAD` per repo. If it's unset, `git remote set-head origin --auto` only with the user's OK (it writes to the repo's git config) |
| commit and PR conventions | `gh pr list --repo ORG/REPO --state merged --limit 30 --json title,headRefName` and `git log origin/<default> -30 --format=%s`. Look for a ticket id format, conventional-commit types and branch names |
| tracker | where the user's tickets live: `gh search issues --assignee @me --state open --limit 20 --json repository,number,title` on the host. If they use a Projects board: `gh project list --owner ORG` (needs `gh auth refresh -s read:project`) and the board's Status field and its done columns (`gh project field-list N --owner ORG`) |
| key prefixes | GitHub issues get keys like `WEB-42`. The default prefix is the repo name in capitals. Pick short prefixes per issue repo if the names are long, and confirm with the user |
| models | after install, `prr models` checks which models your Claude setup can use (a few cents) |

Then ask the user, in one round, only for what's still open. Usual questions:
- Which orgs should the review agent watch? (default: every project org)
- Where do the tickets come from: issues assigned to them across the org, specific repos, or one Projects board? Which columns count as done?
- The PR title and commit format for ticket work (show what you found, e.g. `fix(WEB-42): ...` vs `fix: ... (#42)`).
- Should the ticket agent check every new ticket automatically (`auto_check: "all"`, about $1 to $2 per check) or only when they click Check (`"none"`)?
- Budgets: per review (default $20 cap), per check ($4 cap), per implementation ($40 cap).
- Draft PRs (default yes).
- Their branch format, from their own recent branches: `feature/<slug>`, `<alias>/<slug>`, `users/<alias>/<slug>`
  and so on. Put it in `ticket.conventions.branch` with the parts filled in, e.g. `<alias>/{key}-{slug}`.
- When a ticket has PRs and they ask for more work, should it add commits to the PR's own branch (`ticket.rework_branch:
  "same"`, the default) or always start a follow-up on a new branch, stacked on the open PR (`"new"`)? Anyone who must
  never commit to an existing branch, or whose reviewers dislike a PR changing under review, wants `"new"`.
- Which statuses mean work is expected on a ticket (e.g. To Do, In Progress, Reopened)? List them in
  `ticket.active_statuses` and a ticket with PRs that moves into one of them from another status (Testing, In Review,
  Done) is flagged as came back. Take the names from their tracker's workflow or board columns. Empty means only a
  ticket that leaves their list and returns is flagged.
- A personal review skill they use in chat, if any (`chat_review_skill`). Empty means `pr-review`.
- AI attribution (`attribution.mode`): `"none"` (the default) keeps commits, PRs and reviews free of any tool
  mention; `"disclose"` has the kit add a `Co-Authored-By: Claude` trailer to every commit and a footer to PR
  descriptions, reviews and replies. Ask whether their employer or the projects they work on require disclosure.

## 2b. Learn how they write and work (their profile)

The kit ships no one's voice or habits. Everything personal goes in `profile/` in their copy of the kit (git
ignores it) and in their `kit.config.json`. Learn it from their own writing first, then confirm with them. Read
only, quote them exactly, and never invent an example.

1. **Gather their writing** on the GitHub host (`<me>` is their login):
   ```bash
   gh search prs --reviewed-by=@me --state=closed --limit 30 --json repository,number   # PRs they reviewed
   gh api repos/<o>/<r>/pulls/<n>/comments --jq '.[] | select(.user.login=="<me>") | .body'   # their inline comments
   gh api repos/<o>/<r>/pulls/<n>/reviews --jq '.[] | select(.user.login=="<me>") | {state, body}'
   gh search prs --author=@me --state=merged --limit 20 --json repository,number,title,body   # their PR text
   git -C <repo> log --author=<email> -40 --format='%s%n%b'                                  # their commits
   ```
   Also read their `~/.claude/CLAUDE.md` (outside the agent-kit block) and any project memory for rules they
   already wrote down. Don't copy those rules into the kit; the profile only adds what the agents need.
2. **Write `profile/voice.md`** from `skills/pr-review/references/voice.md`: how they open a comment, how
   they ask for a change, length, case, greetings or thanks or none, words they use and avoid, and 2 to 4 of
   their real review comments quoted as written (pick ones that ask for a change). Keep the "Rules for every
   voice" section. If they have almost no review history, ask them to describe their style and show a draft.
3. **Write `profile/rules.md`**: bullet lines for the "How I work" part of their CLAUDE.md block. Code
   comment style, commit and PR description style, branch habits (e.g. never committing to an existing
   branch), test habits, things to never do on PRs. Only what their writing and answers support.
4. **Ask their review policy** and put it in `kit.config.json`:
   - `review.policy.approve`: `silent` (empty body, no comments) or `note` (a short note when useful).
   - `review.policy.minor`: `private` (minor points only in the summary on the desk) or `post` (posted as
     optional comments).
   - `style`: which switches `post_review.py` enforces on reviews and PR bodies: `no_emoji`, `no_headers`,
     `no_bold`, `no_tables`, `no_bullets`, `no_em_dash`, `no_thanks_opener`, `no_filler`,
     `no_severity_labels`, `max_words_inline`, `max_words_body` (0 means no limit). Propose values from
     their own writing (e.g. they never use emoji or headers) and let them change them.
   - Optionally `profile/severity.md`: their own bar for what blocks a PR, as a `## Severity` section. It goes
     into their `references/policy.md` and overrides the kit's blocking list where they differ.
5. **Show them** the voice summary, the rules and the policy in a few lines each, change what they say, and
   only then install. Back up `profile/` somewhere of their own if they want it on another machine.

## 3. Write the config and install

1. Copy `kit.config.example.json` to `kit.config.json` in the kit folder and fill it in. Field meanings
   are in `kit.json` under `config_reference`. Leave `binaries`, `run_path` and `scheduler` empty to
   auto-detect, unless detection picks the wrong thing.
2. For the conventions, the PR title format may use `{type}`, `{key}`, `{num}` and `{summary}`. The
   ticket agent validates every PR title and commit against it before pushing.
3. Dry run, and show the user the plan (files it creates, files that differ):
   ```bash
   python3 install.py --config kit.config.json
   ```
   If a file shows `differs`, look at what's there before using `--force`. On a machine that already has
   `prr` or `ptk` from somewhere else, stop and ask.
4. Install: `python3 install.py --config kit.config.json --apply`. It writes to `~/.claude/`
   (skills, agents, agent-lib, and a marked block in `CLAUDE.md`), links `~/.local/bin/prr` and `ptk`, and
   writes the scheduler files without loading them. Check `~/.local/bin` is on the user's PATH.

## 4. Map each project into its reference (the step that decides quality)

If the kit folder has `references/<id>.md` for a project (git-ignored, from an earlier `package.py` run or a
zip someone shared), it was mapped before: install seeds it with their own workspace path, so check it's still
true (run the Verify commands once) instead of mapping from scratch. A
project reference holds facts about the repos only (build, test, house patterns, with `path:line`), never one
person's preferences: those go in their profile. Otherwise, fill
`~/.claude/skills/pr-review/references/<id>.md`, following
`references/MAPPING.md` exactly. Read-only. For a big project, use parallel read-only Explore agents,
one per repo, and tell each to report patterns only with two `path:line` examples.
- Run every Verify command once from a worktree and record known failures on the default branch.
- Put the project's own enforced rules (tests or CI that guard logging, routes, migrations) under "What to check".
- If repos pin each other by commit, write the `<workspace>/.local/hooks/pythonpath.sh` hook.
- Show the user a short summary of each reference and ask if anything is wrong.

## 5. Check it works

```bash
prr doctor
ptk doctor
prr models          # with the user's OK, a few cents
ptk models          # the ticket agent keeps its own list
prr settings; ptk settings
```

Fix every FAIL line. Typical ones are in `docs/lessons.md` (credentials from the shell, PATH for node or
uv, gh scopes, origin/HEAD not set).

Then, with the user's OK:
- **A test review:** `prr review <a small open PR the user picks>`. A small PR costs about $1 to $4 and
  takes 10 to 30 minutes. Read the draft with `prr show <pr>`. It must not have posted anything, the posting
  checks must pass, and an approve must have an empty body and no comments (minor points show under
  `Not posted:` in the summary).
- **A test ticket check:** `ptk cycle` picks up the assigned tickets (and with `auto_check: "all"` checks up
  to two). Or `ptk check <KEY>` for one. Look at it on the desk.
- **The dashboards:** `prr dashboard` and `ptk dashboard` open them in the browser (local only, token in
  the URL). Open a ticket, check the Ticket tab shows the issue text and images, and the Model and effort
  section works.

## 6. Start the scheduler

Only after the doctors pass and the user says go:

```bash
python3 install.py --config kit.config.json --apply --load
```

On macOS this loads two launchd jobs, and on Linux it enables two systemd user timers (or cron lines).
The first review cycle only marks the pending review requests as seen (the baseline). From then on, every
new request gets a draft. Tell the user that `prr pause` and `ptk pause` stop the automatic runs.

## 7. Report

Tell the user, in a few short lines: what's installed and where, how to open both dashboards, what each
doctor said, what the test review and check showed, what each run costs, and anything you couldn't verify.
Save a short project memory note if this machine uses memory, with the dashboards, the commands and the
known traps you hit. Then run `python3 package.py`, so the kit folder's git-ignored `references/` keeps the
maps you made for the next install.

## 8. Every later change

Change the kit folder, never the installed copies in `~/.claude`. Then `install.py --apply --force`, bump
`version` and add a line to `changes` in `kit.json` and `CHANGELOG.md`, update `docs/`, run
`python3 package.py` (it must end "up to date" with `--check`), run the tests
(`.venv/bin/python -m pytest tests`, see CONTRIBUTING.md), and open a PR. CI runs the same tests on it.

## Getting updates on a machine that has the kit

`git pull`, then `python3 install.py --config kit.config.json --apply --force`. Config, state, drafts and
project references are kept. Restart the desks (`prr dashboard stop`, `ptk dashboard stop`, then open them
again) so they serve the new code. A desk that is down comes back by itself on the next scheduled cycle, unless you
stopped it.
