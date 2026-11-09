#!/usr/bin/env python3
"""Check a review draft against the PR's real diff and the writing rules, then post it as one review.

usage: post_review.py <draft.json>            check only, prints a preview
       post_review.py <draft.json> --post     check, then submit

Draft shape:
{
  "repo": "acme/orders-api", "pr": 2, "host": "github.com",
  "reviewed_head": "<full sha the review was done on>",
  "event": "APPROVE" | "REQUEST_CHANGES" | "COMMENT",
  "body": "Reviewed and tested locally: ...",
  "comments": [
    {"path": "src/a.ts", "line": 42, "start_line": 40, "side": "RIGHT",
     "body": "... I'd ...\n\n```suggestion\n...\n```",
     "replaces": ["<current text of line 40>", "..."],
     "blocking": true,
     "evidence": ["src/a.ts:42 reads user.email before the null check at src/a.ts:47", "ran the spec: TypeError"]}
  ]
}
"evidence", "replaces" and "blocking" are never posted. Every comment needs evidence: no proof, no comment.
A comment with a suggestion needs "replaces", so a suggestion anchored on the wrong line can't go out.
What gets posted follows references/policy.md (from the config): by default every comment is blocking and an
APPROVE has an empty body and no comments. Under attribution.mode disclose the body ends with the footer.

On a re-review, earlier conversations the new commits fixed can be resolved with the review:
  "resolve_threads": [{"id": "PRRT_...", "path": "src/a.ts", "line": 42, "comment": "null check before email",
                       "evidence": ["src/a.ts:41 now returns early when user is null"]}]
Only open conversations you started are resolved, each needs evidence, and nothing is resolved until the
review itself has been submitted.
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
ATTRIBUTION = re.compile(r"co-authored-by|generated with|claude code|claude\.ai|as an ai|ai assistant", re.I)
FENCE = re.compile(r"^(`{3,})(\w*)\s*$")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# the person's review policy and style switches, written by install.py from their config
_POLICY_FILE = Path(__file__).resolve().parent.parent / "references" / "policy.json"
try:
    _P = json.loads(_POLICY_FILE.read_text())
except (OSError, ValueError):
    _P = {}
POLICY = {"approve": "silent", "minor": "private", **(_P.get("policy") or {})}
STYLE = _P.get("style") or {}
ATTR = {"mode": "none", **(_P.get("attribution") or {})}
DISCLOSE = ATTR["mode"] == "disclose"
LABEL = re.compile(r"^\s*(critical|blocking|blocker|major|nit|nitpick|issue|bug|risk|suggestion|question|q)\s*[:\-]", re.I)


def gh(host, *args, stdin=None):
    out = subprocess.run(["gh", "api", "--hostname", host, *args], input=stdin,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    return out.stdout


def gh_json(host, *args):
    return json.loads(gh(host, *args))


THREADS_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
reviewThreads(first:100){nodes{id isResolved isOutdated path line originalLine comments(first:1){nodes{author{login} body}}}}}}}"""
RESOLVE_M = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}"


def review_threads(host, repo, pr):
    owner, name = repo.split("/", 1)
    data = gh_json(host, "graphql", "-f", f"query={THREADS_Q}", "-f", f"o={owner}", "-f", f"r={name}", "-F", f"n={pr}")
    return {t["id"]: t for t in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]}


def threads_to_resolve(host, repo, pr, viewer, wanted, errors, warnings):
    """The conversations this review may resolve: open, started by you, with proof the fix is in."""
    if not wanted:
        return []
    threads = review_threads(host, repo, pr)
    out = []
    for i, t in enumerate(wanted, 1):
        where = f"resolve {i} ({t.get('path')}:{t.get('line')})"
        th = threads.get(t.get("id") or t.get("thread_id"))
        if not th:
            errors.append(f"{where}: no such conversation on this PR")
            continue
        first = (th["comments"]["nodes"] or [{}])[0]
        if (first.get("author") or {}).get("login") != viewer:
            errors.append(f"{where}: someone else started this conversation, only yours are resolved")
            continue
        if th["isResolved"]:
            warnings.append(f"{where}: already resolved, skipped")
            continue
        ev = t.get("evidence") or []
        ev = [ev] if isinstance(ev, str) else ev
        if not any(re.search(r"[\w./-]+:\d+", e) for e in ev):
            errors.append(f"{where}: no evidence with a path:line that shows the fix")
            continue
        out.append({"id": th["id"], "path": th["path"], "line": th.get("line") or th.get("originalLine"),
                    "asked": (first.get("body") or "").strip().split("\n")[0][:100], "evidence": ev})
    return out


def pr_files(host, repo, pr):
    files, page = [], 1
    while True:
        batch = gh_json(host, f"repos/{repo}/pulls/{pr}/files?per_page=100&page={page}")
        files += batch
        if len(batch) < 100:
            return files
        page += 1


def hunks_of(patch):
    """List of hunks, each {'right': set, 'left': set}."""
    hunks, cur, left, right = [], None, 0, 0
    for raw in (patch or "").split("\n"):
        m = HUNK.match(raw)
        if m:
            cur = {"right": set(), "left": set()}
            hunks.append(cur)
            left, right = int(m.group(1)), int(m.group(3))
            continue
        if cur is None or raw == "" or raw.startswith("\\"):
            continue
        tag = raw[:1]
        if tag == "+":
            cur["right"].add(right); right += 1
        elif tag == "-":
            cur["left"].add(left); left += 1
        else:
            cur["right"].add(right); cur["left"].add(left); right += 1; left += 1
    return hunks


def split_prose(text):
    """Return (prose_text, [(fence_lang, block_lines)])."""
    prose, blocks, fence, lang, buf = [], [], None, "", []
    for line in text.split("\n"):
        m = FENCE.match(line.strip())
        if fence is None and m:
            fence, lang, buf = m.group(1), m.group(2), []
        elif fence is not None and line.strip() == fence:
            blocks.append((lang, buf)); fence = None
        elif fence is not None:
            buf.append(line)
        else:
            prose.append(line)
    if fence is not None:
        blocks.append(("__unclosed__", buf))
    return "\n".join(prose), blocks


def lint_text(where, text, errors, warnings, inline, structure=True):
    """The kit's own rules always; the rest are the person's style switches from policy.json. structure=False is for
    text that follows a repo's own template (a PR description): its headers, bold, tables, checkboxes and length
    belong to the template, so only the wording is checked."""
    prose, blocks = split_prose(text)
    if any(lang == "__unclosed__" for lang, _ in blocks):
        errors.append(f"{where}: unclosed code fence")
    if not DISCLOSE and ATTRIBUTION.search(text):
        errors.append(f"{where}: tool or AI attribution")
    on = lambda k: bool(STYLE.get(k)) and (structure or k not in ("no_headers", "no_bold", "no_tables", "no_bullets"))
    if on("no_emoji") and EMOJI.search(text):
        errors.append(f"{where}: emoji")
    if on("no_headers") and re.search(r"^\s{0,3}#{1,6}\s", prose, re.M):
        errors.append(f"{where}: markdown header")
    if on("no_bold") and re.search(r"\*\*[^*\n]+\*\*|__[^_\n]+__", prose):
        errors.append(f"{where}: bold text")
    if on("no_tables") and re.search(r"^\s*\|.*\|\s*$", prose, re.M):
        errors.append(f"{where}: table")
    if on("no_bullets") and re.search(r"^\s*([-*+]|\d+\.)\s", prose, re.M):
        warnings.append(f"{where}: bullet list, write it as one or two plain sentences")
    if on("no_em_dash") and ("\u2014" in prose or " -- " in prose):
        warnings.append(f"{where}: em dash, split into two sentences")
    if on("no_thanks_opener") and re.match(r"\s*(thanks|thank you|thx|ty)\b", prose, re.I):
        errors.append(f"{where}: starts with thanks, start with the requested change itself")
    if on("no_severity_labels") and LABEL.search(prose):
        warnings.append(f"{where}: severity label, comments have no prefix")
    if on("no_filler") and re.search(r"\b(LGTM|great (job|work)|nice (job|work)|I noticed|it seems|you might want|consider (using|adding|changing))\b", prose, re.I):
        warnings.append(f"{where}: filler phrase, say the case and the ask directly")
    words = len(prose.split())
    limit = (STYLE.get("max_words_inline" if inline else "max_words_body") or 0) if structure else 0
    if limit and words > limit:
        warnings.append(f"{where}: {words} words of prose, keep it under {limit}")
    return blocks


def with_footer(text):
    """Under attribution.mode disclose, what gets posted ends with the footer, once."""
    if not DISCLOSE or ATTR["footer"] in text:
        return text
    return (text.rstrip() + "\n\n" if text.strip() else "") + ATTR["footer"]


def review_payload(head, event, body, comments):
    return {"commit_id": head, "event": event, "body": with_footer(body), "comments": comments}


def event_errors(event, body, comments, own_pr=False, policy=None):
    """The verdict against the blocking flags and the person's review policy."""
    policy = policy or POLICY
    errors = []
    if event not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
        errors.append(f"event must be APPROVE, REQUEST_CHANGES or COMMENT, got {event!r}")
    if own_pr and event in ("APPROVE", "REQUEST_CHANGES"):
        errors.append("this is your own PR, GitHub only allows COMMENT")
    if not body.strip() and (event == "COMMENT" or (event == "REQUEST_CHANGES" and not comments)):
        errors.append(f"{event} needs a body or inline comments")
    blocking = [c for c in comments if c.get("blocking")]
    if event == "REQUEST_CHANGES" and not blocking:
        errors.append("REQUEST_CHANGES with no comment marked blocking: approve instead")
    if event == "APPROVE" and blocking:
        errors.append("APPROVE with a comment marked blocking: request changes instead")
    if event == "APPROVE" and comments and not blocking and policy["minor"] != "post":
        errors.append("an approve has no inline comments here: minor points go in the summary under Not posted (references/policy.md)")
    if event == "APPROVE" and body.strip() and policy["approve"] == "silent":
        errors.append("an approve goes out silent: leave the body empty (references/policy.md)")
    return errors


def comment_policy_errors(where, c, event, prose, policy=None):
    """Whether this comment may be posted at all under the person's review policy."""
    policy = policy or POLICY
    errors = []
    if policy["minor"] != "post" and prose.lstrip().lower().startswith("minor:"):
        errors.append(f"{where}: starts with 'Minor:', only needed changes are posted")
    if not c.get("blocking") and event != "COMMENT" and policy["minor"] != "post":
        errors.append(f"{where}: not blocking, only needed changes are posted (put it in the summary under Not posted)")
    return errors


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    draft = json.load(open(sys.argv[1]))
    post = "--post" in sys.argv[2:]
    if post and os.environ.get("PR_REVIEW_NO_POST"):
        print("ERROR: posting is off in unattended runs, post it with: prr post <pr>")
        sys.exit(1)
    host = draft.get("host") or os.environ.get("GH_HOST") or "github.com"
    repo, pr = draft["repo"], int(draft["pr"])
    event = draft.get("event", "").upper()
    body = draft.get("body", "") or ""
    comments = draft.get("comments", [])
    errors, warnings = [], []

    pull = gh_json(host, f"repos/{repo}/pulls/{pr}")
    viewer = gh_json(host, "user")["login"]
    head = pull["head"]["sha"]
    author = pull["user"]["login"]

    if pull["state"] != "open":
        errors.append(f"PR is {pull['state']}{' (merged)' if pull.get('merged') else ''}")
    if pull.get("draft"):
        warnings.append("PR is a draft")
    if draft.get("reviewed_head") and draft["reviewed_head"] != head:
        errors.append(f"PR moved since the review: reviewed {draft['reviewed_head'][:12]}, head is now {head[:12]}")
    if not draft.get("reviewed_head"):
        warnings.append("no reviewed_head in the draft, can't tell if the PR moved")
    errors += event_errors(event, body, comments, own_pr=(author == viewer))

    lint_text("body", body, errors, warnings, inline=False)
    resolve = threads_to_resolve(host, repo, pr, viewer, draft.get("resolve_threads") or [], errors, warnings)

    files = {f["filename"]: f for f in pr_files(host, repo, pr)}
    raw_cache = {}

    def head_lines(path):
        if path not in raw_cache:
            q = urllib.parse.quote(path, safe="/")
            raw_cache[path] = gh(host, "-H", "Accept: application/vnd.github.raw",
                                 f"repos/{repo}/contents/{q}?ref={head}").split("\n")
        return raw_cache[path]

    previews = []
    for i, c in enumerate(comments, 1):
        where = f"comment {i} ({c.get('path')}:{c.get('line')})"
        path, line, start = c.get("path"), c.get("line"), c.get("start_line")
        side = c.get("side", "RIGHT").upper()
        text = c.get("body", "") or ""
        if not text.strip():
            errors.append(f"{where}: empty body"); continue
        ev = c.get("evidence") or []
        if isinstance(ev, str):
            ev = [ev]
        if not any(re.search(r"[\w./-]+:\d+", e) for e in ev):
            errors.append(f"{where}: no evidence with a path:line, prove it from the code or drop it")
        prose, _ = split_prose(text)
        if not re.search(r"`[^`]+`|[\w./-]+\.\w+:\d+|\bline \d+", prose):
            warnings.append(f"{where}: the comment names no symbol or line, point the owner at the code that proves it")
        errors += comment_policy_errors(where, c, event, prose)
        blocks = lint_text(where, text, errors, warnings, inline=True)
        if path not in files:
            errors.append(f"{where}: file is not in the PR"); continue
        hunks = hunks_of(files[path].get("patch"))
        if not hunks:
            errors.append(f"{where}: GitHub shows no diff for this file (binary or too large)"); continue
        key = "right" if side == "RIGHT" else "left"
        hunk = next((h for h in hunks if line in h[key]), None)
        if hunk is None:
            errors.append(f"{where}: line {line} ({side}) is not in the diff, anchor on a changed line"); continue
        if start is not None and (start >= line or start not in hunk[key]):
            errors.append(f"{where}: start_line {start} must be before line {line} and in the same hunk"); continue
        sugg = [b for lang, b in blocks if lang == "suggestion"]
        if sugg and side != "RIGHT":
            errors.append(f"{where}: suggestions only work on the RIGHT side")
        if len(sugg) > 1:
            errors.append(f"{where}: one suggestion block per comment")
        if sugg and side == "RIGHT":
            src = head_lines(path)
            old = src[(start or line) - 1:line]
            new = sugg[0]
            if old == new:
                errors.append(f"{where}: suggestion is identical to the current code")
            want = c.get("replaces")
            if want is None:
                errors.append(f"{where}: add \"replaces\" with the current text of the line(s) the suggestion replaces")
            else:
                want = [want] if isinstance(want, str) else want
                if [w.strip() for w in want] != [o.strip() for o in old[:len(want)]]:
                    errors.append(f"{where}: anchored on the wrong line, lines {start or line}-{line} are: "
                                  + " | ".join(o.strip() for o in old))
            if old and new and new[0].strip():
                lead = lambda s: s[:len(s) - len(s.lstrip())]
                if lead(old[0]) != lead(new[0]):
                    warnings.append(f"{where}: suggestion indent differs from the line it replaces")
            previews.append((where, old, new))

    api_comments = []
    for c in comments:
        out = {"path": c["path"], "body": c["body"], "line": c["line"], "side": c.get("side", "RIGHT").upper()}
        if c.get("start_line") is not None:
            out["start_line"] = c["start_line"]
            out["start_side"] = c.get("start_side", out["side"]).upper()
        api_comments.append(out)

    print(f"{repo}#{pr} by {author}, head {head[:12]}, event {event}, {len(comments)} inline comment(s)")
    for where, old, new in previews:
        print(f"\n{where} replaces:")
        for l in old:
            print(f"  - {l}")
        for l in new:
            print(f"  + {l}")
    for t in resolve:
        print(f"\nresolves {t['path']}:{t['line']}: {t['asked']}\n  fixed: {t['evidence'][0]}")
    if DISCLOSE:
        print(f"\nthe review body ends with: {ATTR['footer']}")
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        print("\nnot posted, fix the errors above")
        sys.exit(1)
    if not post:
        print("\nchecks passed, run again with --post to submit")
        return
    payload = review_payload(head, event, body, api_comments)
    res = json.loads(gh(host, "-X", "POST", f"repos/{repo}/pulls/{pr}/reviews", "--input", "-",
                        stdin=json.dumps(payload)))
    print(f"\nposted {res.get('state')}: {res.get('html_url')}")
    # only after the review is in, so a failed post never leaves conversations resolved
    for t in resolve:
        try:
            gh(host, "graphql", "-f", f"query={RESOLVE_M}", "-f", f"id={t['id']}")
            print(f"resolved {t['path']}:{t['line']}")
        except RuntimeError as exc:
            print(f"warning: couldn't resolve {t['path']}:{t['line']}: {exc}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"ERROR: gh api failed: {exc}")
        sys.exit(1)
