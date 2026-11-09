# Suggestion blocks (the requested code change)

Every comment carries the change the owner should make, so they can click "Commit suggestion".

    `storage.delete_key` hides every failure, auth errors included, so a revoked key looks like a clean delete. Only the not-found case should be ignored here.

    ```suggestion
            except NotFoundError:
                return
    ```

Mechanics, all checked by `post_review.py`:
- The block replaces lines `start_line`..`line` (or just `line`) on the RIGHT side, in full. Write the
  complete replacement with the exact indentation of the file.
- Every line in that range must be in the diff (added or context lines), and in one hunk.
- Set `replaces` in the draft to the current text of those lines. The script refuses a mismatch, which
  is how an off-by-one anchor gets caught.
- An empty suggestion block deletes the lines.
- One suggestion per comment, only the lines that change.
- If the replacement contains ``` itself, fence the suggestion with four backticks.
- When the fix is outside the diff or spans files, anchor the comment on the nearest changed line and
  show the change in a normal code block with the file path above it, e.g. `In src/worker/retry.py:674:`.
  When the same fix applies to a duplicate copy, say so in one sentence: `Same fix in src/worker/retry.py.`
- Before posting, apply every suggestion in the review worktree and run the typecheck and the touched
  tests, then `git -C <worktree> restore .`. A suggestion that doesn't compile is worse than none.
