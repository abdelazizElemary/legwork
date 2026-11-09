# Changelog

The newest first. `kit.json` has the same list.

## 2.0.0

The first public release, under a new name: legwork. It keeps track of your daily assigned tasks and the PRs waiting for your review, and does the legwork on them with Claude Code. Nothing else about how it runs changes: the `prr` and `ptk` commands, the install paths under `~/.claude`, the scheduler labels and the CLAUDE.md markers are the same, so `install.py --apply --force` upgrades an existing install in place.

Your project maps stay yours. `references/` is git-ignored now: `package.py` still copies the maps you made into it and `install.py` still seeds from it, but they never go into the repo. Its zip says when it carries them, so you only share it with people who may see those repos.

An attribution setting. `attribution.mode` is `"none"` by default, which is how the kit has always worked: no tool or AI mention in commits, PR text, reviews or replies. `"disclose"` is for people whose employer or project asks for disclosure. The kit's own code adds it, so it never depends on the model: the git guard adds a `Co-Authored-By: Claude` trailer to every commit a run makes (git 2.32+), and reviews, PR descriptions and replies end with a footer. Both texts are configurable.

For everyone: an MIT license, a README written for people who haven't seen the kit before, SECURITY.md (report privately through GitHub), the Contributor Covenant as the code of conduct, and bug and feature templates.

## 1.19.1

The conversation with the agent is kept and shown in full. Every exchange on a ticket's Tell box now stays in the ticket's state (the last 100), and exchanges from before that come back from the agent's log and their saved plans (the log kept a message's first 120 characters, so those end with "…"). The desk shows the thread like Messages: a time over each exchange, what you wrote and what the agent answered in full, an earlier plan folded to one line that opens on a tap, and what came of it ("✓ Noted · Rework started · Today 09:30", "Not acted on", "Dismissed"). The last three exchanges are open and the earlier ones are behind "Show earlier messages". A plain answer no longer shows a "Nothing to change" plan. Fixed: a finished exchange was squeezed to one line, which clipped its bubble mid-line.

## 1.19.0

Both desks look Apple-made. The interface uses the system font (SF on a Mac) and Apple's system colours, with the accessible variants wherever text sits on a colour: every text colour passes 4.5:1 on every surface, in light and dark, and Increase Contrast darkens text and lines further. White sheets sit on Apple's light gray page (black and dark gray in Dark Mode), buttons are capsules with one filled primary, Sprint/Stage, the organisation switch, the desk switch and the ticket's tabs are segmented controls, and the toolbar is translucent and a single 56px row from 1000px wide. The ticket list selects with a rounded inset highlight, like a sidebar. The Tell box works like Messages: a round attach button, a rounded field and a round send button, what you wrote in a blue bubble and the agent's answer in a gray one. There are no web fonts any more (the page no longer allows them either) and no theme switch: the desks follow the system appearance.

From the design review of the ticket desk: the counts in the sentence at the top are whole phrases you can click (they were 9px wide); the plan's button says what it starts ("Start rework on #42", "Link #42 and rework", "Park it") and a paid run says what it can cost; the fixed parts take 140px instead of 258px of the window; the composer suggests a few ways to start for the ticket's state (a tap fills the box, it never sends); the list says "PR approved" so it isn't confused with approving the implementation; the model and effort for the run whose button is on the card aren't offered a second time below it; and linking a PR replaces the earlier run's summary, which said linking was impossible.

## 1.18.2

One composer on every ticket. The Tell box looks and works the same whatever state the ticket is in: the text box, a paperclip button for screenshots and Send in one row, and a one-line hint under it, always visible. Before, Send and "Attach screenshots" showed only while the box had focus or a plan was open, so some tickets seemed to have them and others not. The placeholder is shorter and stays on one line on a phone.

## 1.18.1

One scroll on the ticket desk. The page is the only thing that scrolls: the ticket pane no longer scrolls on its own, the Tell exchange shows in full above the input instead of in a capped box, and the input grows with what you type (only a message over half the screen gets a scrollbar) while it stays pinned to the bottom of the screen. Opening a ticket brings its top into view, a tab switch keeps the tabs in view, and a new plan is brought into view above the input (a tall one from its start). Code in the ticket, commands and error output wrap instead of scrolling sideways; only a wide table in the ticket text still does.

## 1.18.0

One input. The ticket pane has a single box, Tell the agent, pinned to its bottom: the rework, fix, answer and note boxes are gone, and whatever you want goes there. Tell can now also save an answer to the agent's questions (`context`), approve a checked ticket (`approve`), run a failed step again (`fix`: resume, start over or only the PR step) and link a PR (`link_pr`). The buttons that need no words stay (Approve, Check, Rework on the same PR, Fix the conflict). A finished exchange shrinks to two lines.

Your own PR, linked. A PR you opened by hand can become the ticket's PR: `link_pr` takes only an open PR in the org's repo, opened by you, not from a fork, and replaces nothing that is still open. Reworks then add commits to its branch, never force-pushed, in the agent's own worktree (`<repo>-<key>-pr<n>`, detached when your checkout has the branch). The run's rules say so too (the skill, and the rule in your profile when it has one). Before, the agent could only add commits to branches it created, so a request to continue on such a PR ended blocked, and every rerun ended the same way.

Every rework starts from the latest base branch. Before the run, the agent fetches the base and merges it into the PR's branch as you. A clean merge is done for the run; a conflict is undone and left to the run, which must merge and resolve it, and the PR step refuses to push until the base is in. `ticket.rework_merge_base: false` turns it off.

No more loops. A failure whose diagnosis says it needs you no longer offers Fix it or Start over, which would stop at the same point again; the desk says to tell the agent instead, and the Tell run reads the diagnosis and must not propose a run that hits the same wall.

## 1.17.0

The desks come back by themselves. Nothing started them after a restart, so they looked down until someone ran `ptk dashboard` again. Each scheduled cycle (every few minutes, and at login) now starts its desk when it isn't up, unless you stopped it with `dashboard stop`; `dashboard` starts it again and lifts that. A desk is up only when its process is alive and its port answers, since after a restart the pid saved in `dashboard.json` can belong to another process. Runs started from a desk (a check, an implementation, a review) get the low CPU priority the scheduler's own runs already have (`nice -n 10`), so the laptop stays usable while one builds and tests.

The ticket pane scrolls as one. A tall head, such as a Tell plan, pushed the only scrolling part below the pane, so nothing in it could be scrolled or read. The pane is the scroller now and keeps a wheel inside it. It is no longer an aria-live region either, since it is redrawn on every poll while a run is going.

## 1.16.0

A real app frame on both desks. The header has two rows: the product (the Agent desks mark, a switch between Reviews and Tickets that shows how many items wait for you on this desk, and the theme), then this desk (its name, what the agent is doing, and its actions). Each desk links to the other while it is running; one that isn't running says how to start it. The footer says what the desks will and won't do and which kit version is installed, and it sits at the bottom of short pages. A new shared `agent-lib/frame.py` reads the version (written by the installer to `agent-lib/kit_version.json`) and the other desk's link from the files the desks already write, behind the same token as the rest of the API.

## 1.15.0

Tell the agent. Every ticket on the ticket desk has one box, under its title: write anything about the ticket in your own words ("I fixed the conflict by hand, check the latest", "close it for now"), with screenshots if you want. A read-only run reads the ticket, its PRs and the tracker, and answers with what it found and a plan made only of the desk's own actions: add a note, park or bring back, mark closed, read the PRs again, check again, fix the merge conflict, rework. Nothing changes until you press Do it, and each action goes through its own checks. A ticket is only marked closed when the tracker shows a done status or every PR is merged or closed, and it comes back on the desk if the tracker reopens it. Skip is called Park now.

A new look for both desks, so they read as one instrument and not a template. Source Sans 3 for the interface, Source Serif 4 for what a person or the agent wrote, mono only for code. Paper and graphite with one ink-blue, state colours as the only saturated things on the page. A turn rail at the left of every row says whose turn it is: amber is yours, ink-blue the agent's, red failed. The stat tiles are now one plain sentence whose counts jump to their groups. The ticket desk goes to one column under 1000px, so the ticket no longer collapses on a phone.

## 1.14.0

A merge conflict on a PR the agent opened gets fixed on that same PR. GitHub's merge status shows the conflict on the ticket desk, with a "Fix the conflict on the same PR" button. The agent merges the base into its own PR branch in its own worktree, resolves the conflict with the instructions you write in the box, runs the tests, and pushes the merge fast-forward. The merge is checked like a rework (your author, the ticket named, no attribution, no secrets), and it is refused unless the base really is merged in. Nothing is force-pushed, and it only touches branches the agent created.

## 1.13.0

A rework starts from GitHub. Before every rework, and before a Fix it during one, the agent reads the PR branch again and brings its worktree level with it: commits pushed by hand (a conflict resolved, a suggestion) are taken, and a force-pushed branch is adopted when the agent has nothing unpushed (otherwise it stops and changes nothing). What changed on GitHub since the last push goes into rework.md and the fix prompt, so "I resolved it, check the latest" works. The agent always works in its own worktree under .local/wt: a path recorded elsewhere is ignored, and when someone else has the branch checked out, the agent uses a detached worktree and pushes to the branch by name. The PR step checks only the rework's own commits (not what a merge of the base branch brings in), and a merge of the base keeps git's own message. A Fix it during a rework runs `/ticket-work rework`, never implement, and "run only the PR step" reads GitHub first too. Also fixed: a run's progress file could be written by two threads at once (the run reader and the model switch watcher), which failed a test now and then on macOS.

## 1.12.0

Ticket PRs follow the repo's own PR template (ticket.pr_template, default "repo"). conventions.md points the run at it; before pushing, the agent checks every section is there, in order and filled, with no placeholder left; the person's style switches judge the wording only (lint_text structure=False); the Refs ticket line is skipped when the body links the ticket already; a rework rewrites an old description into the template; fix: a failed PR check whose commit id contained 401, 403 or 529 was explained as a sign-in, model or rate problem (PR check errors are explained first now, and status codes match whole numbers).

## 1.11.0

A rework answers PR comments in place instead of in the PR description. rework.md gives each comment a reply id; the run writes replies into the manifest; the agent checks them before pushing (a real comment on the ticket's PRs, not a bot, once, the person's style) and refuses a PR body that quotes or addresses a reviewer; after the push it replies in the review thread for inline comments and with a quoting PR comment for the others, also on earlier PRs in follow-up rounds ({new_pr}); the desk marks comments Replied and lists replies under each rework.

## 1.10.1

Came back from testing. A new setting, `ticket.active_statuses`, lists the statuses that mean work is expected (To Do, In Progress, Reopened). A ticket with PRs that moves into one of them from any other status (Testing, In Review, Done) between two polls is flagged as came back: the desk marks it, notifies once, and the rework form says why. It is off while the list is empty, so nothing changes until it is set, and a ticket that leaves the list and returns is flagged as before. Starting the rework clears the flag. The setup runbook asks for both this and `rework_branch`.

## 1.10.0

Follow-up rounds. A new setting, `ticket.rework_branch` (`same` is the default and changes nothing, `new`), lets a rework always go on a new branch: the earlier PR is never touched, the new branch is stacked on it while it is open, or starts from the latest default branch once it is merged or closed. Each rework is a numbered round with its own worktree (`ticket_worktree.sh --round N`, `--base B`), `rework.md` lists the earlier PRs, where each new branch starts and the ticket's comments since the last push, and the skill has a follow-up section. The PR step accepts a base that is the default branch or an open earlier PR branch of the same ticket and nothing else, explains a base that was deleted meanwhile, and a failed follow-up is retried as the same round. Install refuses a bad value. Also fixed: a second branch for the same ticket and repo failed because the worktree was reused, and a follow-up PR's `rework.md` listed none of the earlier PRs.

## 1.9.0

Screenshots on a rework. The ticket desk's rework form takes images (choose files, or paste them into the box), and `ptk rework <KEY> "..." --image FILE` does the same from the terminal. They are saved in the ticket's attachments as `from-you-...` (PNG, JPEG, GIF or WebP up to 12 MB, checked by content, never SVG or anything renamed), `rework.md` lists them, and the rework skill reads each one first. Only images already in the ticket's own attachments can ride along on a rework request.

## 1.8.1

Refresh PR status on demand. A "Refresh PR status" button in the ticket desk header reads every ticket's PRs from GitHub (draft, ready, approved, merged, closed), each ticket's PR card has a "Refresh now" link, `ptk prs [<KEY>]` does it from the terminal, and opening a ticket whose PRs were last read more than two minutes ago reads them in the background. A failed read keeps the last known state and shows the error; a running ticket, or one changed while GitHub was being read, is left alone. Read-only: nothing is written to GitHub.

## 1.8.0

Safe contributions. A test suite (152 tests, no Claude or GitHub needed) covers the gh and git guards, the posting checks, ticket pushes and rework, agent decisions, the live switch, the desks' HTTP security, the installer and repo hygiene (no secrets or personal data, versioned releases); CI runs it on every PR on Linux and macOS with pinned actions and a read-only token; CODEOWNERS, a PR template, CONTRIBUTING.md and Dependabot. Guard fixes: gh api -XPOST, GraphQL mutations in query files, gh auth/extension/ruleset writes, and git --git-dir/--work-tree/GIT_DIR, remote and config changes can no longer reach GitHub or a person's checkout.

## 1.7.0

Nothing personal ships in the kit. Each person's voice, working habits and review bar live in profile/ (git-ignored) and their config; setup learns them from their own review comments, PR text and commits and confirms them (IMPLEMENT.md 2b). review.policy (approve silent or note, minor points private or posted) and style switches (emoji, headers, bold, tables, em dashes, thanks openers, filler, word limits) are per person and enforced by post_review.py from the generated references/policy.json; voice.md is a neutral template; suggestion mechanics moved to references/suggestions.md; project references hold repo facts only; lessons and README carry no one's history.

## 1.6.0

Shared privately for early use; a README for new users (clone, set up, costs, safety, updates, changing it); CHANGELOG.md; mapped project references are stored with placeholders ({{WORKSPACE}}, {{AUTHOR_FIRST}}, {{MEMORY_INDEX}}) and rendered from each person's config; setup asks about the branch naming, the review voice and a personal chat review skill; the coverage tools setup is in the reference; the installer skips cache files.

## 1.5.1

Review comments start with the requested change itself, never with thanks; post_review.py refuses a comment or body that opens with thanks.

## 1.5.0

The ticket desk follows each PR on GitHub (draft or ready, review decision, merged or closed, CI, comments and reviews, bot reports apart) and the ticket's phase follows them (pr_open, merged, pr_closed); tickets that come back on the board are flagged; Rework (desk button or ptk rework) adds commits to the same PR branch the agent created, fast-forward only and checked like a new PR, or opens a follow-up PR when merged; pause no longer stops reading the tracker and PRs; tracker timeouts no longer crash the cycle; pause and resume are logged.

## 1.4.0

Parallel runs (slot pools per job, queue ordered by sprint and priority); model and effort pickers next to every run button, and a change switches running agents live (stream-json control: set_model, apply_flag_settings) for both agents; failed runs get a plain explanation at once, an automatic read-only diagnosis (what happened, why, evidence, fix), and Fix it / Start over / PR step only buttons that resume the same session; Claude usage limits resume on their own after the reset; package.py keeps the kit in sync and zips it; mapped project references ship in references/.

## 1.3.1

GitHub Projects tickets carry their sprint (iteration field); the ticket desk and ptk list group by sprint (current, upcoming, past, none) and sort by priority, with a Sprint/Stage switch.

## 1.3.0

Configurable ticket branch format; review voice can send REQUEST_CHANGES with an empty body; a project reference's Severity section overrides the blocking list; desks get a glance strip, a theme switch and a sticky header.

## 1.2.0

Re-reviews resolve your earlier conversations the author fixed, when you post, with proof.

## 1.1.0

Reviews approve silently (no body, no comments, minor points stay private); the desks show live progress of every Claude run.
