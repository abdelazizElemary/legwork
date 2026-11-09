# Rules for my project repos

These apply to every repo under {{WORKSPACE_LIST}} (GitHub orgs {{ORG_LIST}} on {{GH_HOST}}).

## Authorship
{{AUTHORSHIP_RULE}}
- Git identity: {{AUTHOR_NAME}} <{{AUTHOR_EMAIL}}>.{{COMMIT_RULE}}

## How I work
{{PERSONAL_RULES}}

## No assumptions
- Back every claim, fix and review comment with a `path:line` from the code (at the commit that
  matters). Label anything unproven as a question, or don't say it.

## PR reviews
- The background review agent uses the `pr-review` skill. When I ask for a review in chat, use {{CHAT_REVIEW}}.
{{REVIEW_POLICY_RULES}}
- On a re-review, my earlier conversations the author fixed get resolved when I post, with proof of the
  fix. Conversations that are only partly fixed, pushed back on, or started by someone else stay open.
- Critical includes two more cases. First, untested backend code on a critical path (business
  decisions, auth, data writes, migrations, a bug fix without a regression test), proven with
  coverage. Second, a clear violation of the repo's own proven architecture. Frontend changes must
  follow the repo's own patterns too. A `Severity` section in my review policy or a project reference overrides this.
- A background agent drafts a review whenever I'm requested as a reviewer, and never posts. To see or
  post one, use `prr list`, `prr show <pr>` and `prr post <pr>`, or the local dashboard from `prr dashboard`
  (code in `~/.claude/pr-review-agent`).

## Tickets
- A ticket agent checks every open ticket assigned to me in {{TRACKER_DESC}} and implements one only after I
  approve it on the ticket desk (`ptk dashboard`, CLI `ptk`, code in `~/.claude/ticket-agent`). It opens draft
  PRs, one per repo, from the latest default branch. The tracker stays read-only. For ticket work use the
  `ticket-work` skill.

## Safety
- Never checkout, pull, reset, restore or stash in my checkouts unasked, because they sit on deliberate
  branches. Use a separate worktree. Spawned agents stay read-only unless I asked for edits.
