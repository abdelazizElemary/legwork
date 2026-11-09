# Security

Legwork runs Claude Code unattended on your machine with your GitHub login, so its guards matter. Thanks for
helping keep them tight.

## Report a vulnerability

Please don't open a public issue. Report it privately on GitHub:
[Report a vulnerability](https://github.com/abdelazizElemary/legwork/security/advisories/new) (the Security tab).
Say which version you run (`version` in `kit.json`), your OS, and the steps that show the problem. You'll get
an answer within a week, and a fix ships as a new release with credit to you unless you'd rather not.

## In scope

- An unattended run getting past the git or gh guards (`agents/agent-lib/shims/`): pushing, posting, writing to
  GitHub, changing the gh login, or changing a checkout of yours outside `<workspace>/.local`.
- The posting and push checks letting through what they should refuse: `skills/pr-review/scripts/post_review.py`,
  and the ticket agent's PR step (`finalize` and `finalize_rework` in `agents/ticket-agent/agent.py`).
- The desks (`dashboard.py`): answering without the token, from another host, or to a request from another site.
- A secret ending up in a log, a desk, a draft, a commit or a PR.

## Out of scope

- What the model writes in a review or a PR. You read a review before you post it, and ticket PRs open as drafts.
- Claude Code, `gh`, git or GitHub themselves. Report those to their own projects.
- A setup where you changed the guards or ran the agents outside the installed scheduler jobs.

## Supported versions

Only the latest release gets fixes.
