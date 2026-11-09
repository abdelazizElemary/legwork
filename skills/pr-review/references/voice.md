# Writing the review in {{AUTHOR_FIRST}}'s voice

<!-- This is the kit's neutral starting point. Setup replaces it with {{AUTHOR_FIRST}}'s own voice, learned
     from their review comments, PR descriptions and commits (IMPLEMENT.md, "Learn how they write"), and
     keeps it in profile/voice.md in their copy of the kit. -->

The review is posted under {{AUTHOR_FIRST}}'s name to teammates. It has to read like {{AUTHOR_FIRST}} wrote it by
hand. What gets posted at all (approves, minor points) is in `references/policy.md`.

## How {{AUTHOR_FIRST}} writes

Until setup learns it: short, plain and polite. Simple English, since teammates may read English as a second
language. The concrete case first, then the ask.

## Real examples of {{AUTHOR_FIRST}}'s comments

None yet. Setup adds two to four of {{AUTHOR_FIRST}}'s own past review comments here, quoted as written.

## Rules for every voice (the kit checks these)

- {{ATTRIBUTION_RULE}}
- Name the exact symbol in backticks and the line or file that proves the point.
- Don't restate what the diff does. The owner wrote it.
- Don't ask teammates to rewrite their comments into {{AUTHOR_FIRST}}'s style. That's for {{AUTHOR_FIRST}}'s own text.
- The style switches in `references/policy.md` (emoji, headers, bold, tables, openers, length) are enforced
  by `post_review.py` before anything is posted.

## Inline comment shape

1. The concrete case: which input or state, at which line, does what, and what goes wrong.
2. The ask, the way {{AUTHOR_FIRST}} asks.
3. The fix as a `suggestion` block (`references/suggestions.md`), or a short code block when it can't be one.

The example below is made up to show the shape, in the neutral voice:

> `retry()` re-sends after a 409 at line 88, so a duplicate order is created when the first request
> already went through. Only 5xx responses should be retried.
