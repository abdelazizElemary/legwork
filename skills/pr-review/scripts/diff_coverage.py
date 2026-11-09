#!/usr/bin/env python3
"""Which lines a PR added or changed are not run by any test, from an lcov report.

usage: diff_coverage.py --worktree <wt> --base <merge-base sha> --lcov <file> [--lcov <file> ...]

Make the lcov first (commands in the project reference), from the same worktree and with the full unit
suite when you can, since a partial run makes untouched modules look untested. Test files, type-only
files and non-code files are skipped. A changed code file missing from the report means no test loaded it.
"""

import argparse
import os
import re
import subprocess
import sys

CODE = re.compile(r"\.(ts|tsx|js|jsx|mjs|cjs|py)$")
SKIP = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|mocks|fixtures|migrations|prisma)/"
    r"|\.(spec|test)\.[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$"
    r"|\.d\.ts$|(^|/)(vite|vitest|jest|next|eslint|playwright|postcss|tailwind)\.config\.[^/]+$"
)


def changed_lines(wt, base):
    out = subprocess.run(["git", "-C", wt, "diff", "--unified=0", "--no-color", "--no-ext-diff", base, "HEAD"],
                         capture_output=True, text=True, check=True).stdout
    files, path = {}, None
    for line in out.split("\n"):
        if line.startswith("+++ "):
            path = None if line[4:] == "/dev/null" else line[6:]
            if path is not None:
                files.setdefault(path, set())
        elif line.startswith("@@") and path is not None:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            start, count = int(m.group(1)), int(m.group(2) or 1)
            files[path].update(range(start, start + count))
    return {p: ls for p, ls in files.items() if ls}


def read_lcov(paths, wt):
    cov, cur = {}, None
    for p in paths:
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("SF:"):
                sf = line[3:]
                sf = os.path.relpath(sf, wt) if os.path.isabs(sf) else os.path.normpath(sf)
                cur = cov.setdefault(sf, {})
            elif line.startswith("DA:") and cur is not None:
                n, hits = line[3:].split(",")[:2]
                cur[int(n)] = max(cur.get(int(n), 0), int(float(hits)))
            elif line == "end_of_record":
                cur = None
    return cov


def ranges(nums):
    out, nums = [], sorted(nums)
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--lcov", action="append", required=True)
    a = ap.parse_args()
    wt = os.path.realpath(a.worktree)
    cov = read_lcov(a.lcov, wt)
    # match report paths that are relative to a sub-root (e.g. coverage run from a package dir)
    by_suffix = {}
    for k in cov:
        by_suffix.setdefault(os.path.basename(k), []).append(k)

    total = covered = 0
    rows, missing = [], []
    for path, lines in sorted(changed_lines(wt, a.base).items()):
        if not CODE.search(path) or SKIP.search(path):
            continue
        rec = cov.get(path)
        if rec is None:
            cands = [k for k in by_suffix.get(os.path.basename(path), []) if path.endswith(k) or k.endswith(path)]
            rec = cov.get(cands[0]) if len(cands) == 1 else None
        if rec is None:
            missing.append(f"{path} ({len(lines)} changed lines)")
            continue
        exe = {n for n in lines if n in rec}
        if not exe:
            continue
        hit = {n for n in exe if rec[n] > 0}
        total += len(exe); covered += len(hit)
        miss = exe - hit
        rows.append(f"{path}: {len(hit)}/{len(exe)} changed lines run by tests"
                    + (f", not run: {ranges(miss)}" if miss else ""))

    for r in rows:
        print(r)
    for m in missing:
        print(f"{m}: not in the coverage report, no test loaded this file")
    if total:
        print(f"\nchanged executable lines run by tests: {covered}/{total} ({100 * covered // total}%)")
    elif not missing:
        print("no changed executable code lines")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: git diff failed: {exc.stderr.strip()}")
        sys.exit(1)
