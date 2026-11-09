# Contributing

Thanks for helping. Legwork runs unattended on people's machines with their GitHub identity, so changes go through
a pull request, the tests and a review. CI runs the same tests on every PR (`.github/workflows/kit-checks.yml`).

For a bug or an idea, open an issue first with the form. A security problem goes to [SECURITY.md](SECURITY.md)
instead. Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## How to change something

1. Fork the repo and work on a branch in your fork. Never edit the installed copies in
   `~/.claude`: change the kit, then `python3 install.py --config kit.config.json --apply --force`.
2. Run the tests:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
   .venv/bin/python -m pytest tests
   ```
   They install the kit into a throwaway home with a fake `claude` and `gh`, so they cost nothing, need no
   login and never touch your own setup. A run takes under a minute.
3. If you changed anything that gets installed (`agents/`, `skills/`, `rules/`, `service/`,
   `install.py`, `package.py`): bump `version` in `kit.json`, add one line to its `changes` and the same entry
   at the top of `CHANGELOG.md`, update `docs/` when behaviour changes, and run `python3 package.py`.
4. Open a PR. Fill in the template, and say what you ran by hand on your own machine.

## What CI checks

| Area | Tests | What they protect |
|---|---|---|
| Guards | `test_shims.py` | Unattended runs can't push, post, write to GitHub, change the gh login, or change a person's own checkout, including through `-XPOST`, query files, `--git-dir`/`--work-tree` and `GIT_DIR` |
| Posting | `test_post_review.py` | What a review may post follows the person's policy and style; attribution and broken fences are always refused |
| Reworks | `test_rework_sync.py`, `test_linked_pr.py`, `test_replies.py`, `test_pr_template.py` | A rework starts from the PR branch on GitHub and the latest base; only the person's own PRs can be linked; replies go where comments were left; the repo's PR template is followed |
| Ticket PRs | `test_ticket_push.py` | Only checked commits by the person, naming the ticket, are pushed; drafts; rework only fast-forwards the agent's own branch and never rewrites pushed commits |
| Agent logic | `test_ticket_agent.py` | Failure explanations, usage-limit resume, priority and sprint order, run slots, fix requests, PR state sync |
| Live switch | `test_live_switch.py` | A model or effort change reaches a running agent, and runs end cleanly |
| Desks | `test_desks.py` | Both dashboards need their token, refuse other hosts and non-JSON posts, send their security headers, stay local |
| Install | `test_install.py` | A teammate gets a neutral kit, a profile makes it theirs, reinstalls keep their config and state |
| Repo | `test_static.py`, `test_hygiene.py` | Everything parses, `kit.json` lists every file, no secrets, no personal data, releases are versioned |

The model's own judgement (what it finds in a PR, how it implements a ticket) isn't tested in CI, since that
needs Claude and costs money. What the kit guarantees whatever the model does is tested: the guards, the
posting checks and the push checks. When you change a prompt or a skill, try it once on your machine and say
so in the PR.

## Rules for every change

- A new way to write to GitHub, push, or run git in someone's checkout needs a guard and a test first.
- Nothing personal: no names, emails, home paths, usage history, costs, or anyone's voice or habits. Those
  live in each person's `profile/`, `kit.config.json` and `references/`, which git ignores. Set
  `KIT_PRIVATE_TERMS` (comma-separated words: your name, alias, work email, employer) when you run the tests, and
  the hygiene test fails if any of them is in a shipped file. CI does the same with a repo secret.
- Keep it standard library only for the agents (pytest is only for the tests).
