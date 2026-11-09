# {{PROJECT_LABEL}} reference

<!-- Fill every section from the code, with path:line proof, following references/MAPPING.md.
     Delete this comment when done. A claim you can't prove from the code doesn't go in. -->

Repos live in `{{WORKSPACE}}/<repo>`. Remote is {{GH_HOST}}, org `{{ORG}}`.
<one line per repo: name, language and framework, what it does, who calls it>

Read first: <project memory index path, or "none">.

## gh and git

- `export GH_HOST={{GH_HOST}}` and pass `--repo {{ORG}}/<repo>`.
- Default branch: <per repo, from `git symbolic-ref refs/remotes/origin/HEAD`>.
- Merge style: <squash, merge or rebase; whether force push is allowed>.
- CI: <what runs on PRs, and what a green check does and doesn't prove>.
- Ticket ids in commits and PR titles: <format, e.g. ABC-123, or none>.

## Verify

`bash ~/.claude/skills/pr-review/scripts/pr_worktree.sh {{WORKSPACE}}/<repo> <N> --with-base`

Per stack, the exact commands that work from a worktree (run each once to prove it):
- Tests: <command, env vars it needs, how long the full suite takes, known failures on the default branch>
- Lint and format: <command>
- Typecheck: <command>
- Build: <command, and when it's worth running>
- Coverage to lcov: <command that writes lcov for the changed files>

Known traps: <venv drift, services the tests need, cwd-dependent tests, order-dependent failures>.

## What to check

Code that exists twice: <modules duplicated across repos, with both paths. A fix in one copy needs the other>.

Rules enforced by tests or CI: <e.g. a logging guard test, a public-routes list, a migrations check. Each with
the test path, so a violation is blocking>.

Tenancy and access: <how tenant or owner filtering works, where it's applied, the column or claim it uses>.

Paths, keys, uploads, downloads: <how user-controlled paths are validated, where the guards live>.

Events and limits: <event caps, throttling, retries, idempotency rules>.

External services: <how they are called (factories, clients), what must not be called directly>.

Error handling: <the house way to fail, log and retry>.

## House architecture: backend

Mapped <date> on <branch>. Re-check the cited lines before quoting them, since they move.

<For each repo or group of repos, one bullet per pattern. Each pattern names at least two places that
follow it, e.g.:>
- **<Pattern in one sentence>.** `<path:line>`, `<path:line>`. <What a PR that breaks it looks like.>

Module shape: <the files a new module or feature has, with an example folder>.

Tests: <framework, where tests live, fixtures and factories, what gets mocked and what stays real, naming>.

## House architecture: frontend

Mapped <date> on <branch>.
- **API layer.** <where calls live, the client, how errors and auth are handled> `<path:line>`, `<path:line>`.
- **UI kit.** <the component library, where it lives, the components to reuse> `<path:line>`.
- **State and data fetching.** <the library and pattern> `<path:line>`.
- **Routing, forms, i18n, accessibility.** <the house way, with examples>.

Tests: <runner, location, what gets mocked>.

## Don't flag

<Settled decisions and known noise that are not findings: legacy code, accepted lint backlog, generated
files, choices recorded in the project memory.>
