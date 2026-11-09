# Prompt: set up my PR review agent and ticket agent

Clone the repo (the Code button on its GitHub page gives the URL), `cd` into it, start `claude`, and say:
"Follow PROMPT.md". You can also paste everything below the line as your first message.

---

I want you to set up two background agents for me on this machine, from the kit in this folder
(`legwork`, a clone of its GitHub repo or an unzipped `legwork.zip`):

1. **A PR review agent** (`prr`, plus a local "Review desk" dashboard). Whenever I'm requested as a
   reviewer on GitHub, it builds and tests the PR in an isolated worktree, checks test coverage of the
   changed lines and the repo's own architecture, and drafts a review in my voice with GitHub suggestion
   blocks. It never posts by itself: I post from the dashboard or with `prr post`.
2. **A ticket agent** (`ptk`, plus a local "Ticket desk" dashboard). It reads the tickets assigned to me
   (GitHub Issues or a GitHub Projects board for this project, read-only), checks whether each one has
   enough context, and asks me questions on the dashboard when it doesn't. After I approve one, it
   implements it on new branches from the latest default branch, one draft PR per repo, with the ticket
   key in the branch, commits and PR titles.

Do it like this:

- Read `IMPLEMENT.md` and follow it step by step. Learn my voice, habits and review style from my own GitHub
  history (step 2b) and confirm them with me; don't use anyone else's. `kit.json` is the manifest of every
  file and setting. `docs/design.md` explains how the parts fit together, and `docs/lessons.md` lists the
  problems hit so far. Don't repeat those mistakes.
- Find everything you can yourself first: my git identity, my GitHub host and login, the workspace folders
  and orgs from the repo remotes, the default branches, the PR title and commit conventions from recent
  merged PRs, and where the issues live. Then ask me only for what you couldn't find, in one short round
  of questions with your best guess as the first option.
- Run `install.py` as a dry run first and show me the plan. Install only after I agree. Don't start the
  scheduler jobs (`--load`) until the doctors pass and I say go.
- The most important step is mapping each project into its reference file
  (`skills/pr-review/references/MAPPING.md`): how to build and test each repo, and the house patterns with
  two `path:line` examples each. Do it read-only, and run every Verify command once to prove it works.
- Anything that costs money (a test review, a ticket check) or writes to GitHub needs my OK first.

The kit's safety rules apply to this setup too: I'm the only author of anything you write for me (no
Co-Authored-By, no "Generated with" lines). Never checkout, pull, reset, restore or stash in my checkouts, use
worktrees. No assumptions: back every claim with `path:line` or a command you ran. The tracker stays read-only.

When you finish, tell me in a few lines what's installed, how to open both dashboards, what each doctor
said, and anything you couldn't verify.
