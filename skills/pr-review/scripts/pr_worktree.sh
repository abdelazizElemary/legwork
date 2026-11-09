#!/usr/bin/env bash
# Set up an isolated review worktree for a PR. Never touches the repo's own checkout or branches.
#
# usage: pr_worktree.sh <repo-dir> <pr-number> [--base <branch>] [--repo <org/name>]
#                       [--deps auto|link|prisma-safe|install|none] [--with-base]
#
# Worktrees go to <workspace>/.local/wt/<repo>-pr<N> (and <repo>-base-<sha7> with --with-base),
# where <workspace> is the repo's parent folder.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: pr_worktree.sh <repo-dir> <pr-number> [--base B] [--repo ORG/NAME] [--deps MODE] [--with-base]"
repo_dir=$(cd "$1" && pwd) || die "no such dir: $1"
pr=$2; shift 2
base=""; slug=""; deps="auto"; with_base=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) base=$2; shift 2 ;;
    --repo) slug=$2; shift 2 ;;
    --deps) deps=$2; shift 2 ;;
    --with-base) with_base=1; shift ;;
    *) die "unknown flag: $1" ;;
  esac
done
[[ "$pr" =~ ^[0-9]+$ ]] || die "pr must be a number: $pr"
git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1 || die "not a git repo: $repo_dir"

remote_url=$(git -C "$repo_dir" remote get-url origin)
host=$(printf '%s' "$remote_url" | sed -E 's#^(ssh://)?(git@|https://)([^:/]+)[:/].*#\3#')
if [ -z "$slug" ]; then
  slug=$(printf '%s' "$remote_url" | sed -E 's#^(ssh://)?(git@|https://)[^:/]+[:/]##; s#\.git$##')
fi
export GH_HOST="$host"

if [ -z "$base" ]; then
  base=$(gh pr view "$pr" --repo "$slug" --json baseRefName -q .baseRefName) \
    || die "gh pr view failed; pass --repo ORG/NAME (fork remotes point at the wrong repo)"
fi

repo_name=$(basename "$repo_dir")
short=$repo_name
ws_root=$(dirname "$repo_dir")
# a repo cloned just for review lives in <workspace>/.local/clones/<repo>
if [ "$(basename "$ws_root")" = "clones" ] && [ "$(basename "$(dirname "$ws_root")")" = ".local" ]; then
  ws_root=$(dirname "$(dirname "$ws_root")")
fi
wt_root="$ws_root/.local/wt"
wt="$wt_root/${short}-pr${pr}"
mkdir -p "$wt_root"

# remote-tracking refs only; local branches are left alone
git -C "$repo_dir" fetch --quiet origin \
  "+refs/pull/${pr}/head:refs/remotes/origin/pr/${pr}" \
  "+refs/heads/${base}:refs/remotes/origin/${base}"
pr_ref="refs/remotes/origin/pr/${pr}"
base_ref="refs/remotes/origin/${base}"

if [ -e "$wt/.git" ]; then
  if [ -n "$(git -C "$wt" status --porcelain --untracked-files=no)" ]; then
    die "review worktree has local edits: $wt (git -C $wt restore . to drop them)"
  fi
  git -C "$wt" checkout --quiet --detach "$pr_ref"
else
  git -C "$repo_dir" worktree add --quiet --detach "$wt" "$pr_ref"
fi

head_sha=$(git -C "$wt" rev-parse HEAD)
base_sha=$(git -C "$repo_dir" rev-parse "$base_ref")
merge_base=$(git -C "$repo_dir" merge-base "$base_ref" "$pr_ref")
behind=$(git -C "$repo_dir" rev-list --count "${pr_ref}..${base_ref}")
ahead=$(git -C "$repo_dir" rev-list --count "${base_ref}..${pr_ref}")

# the lockfile when the repo tracks one, otherwise the declared deps (some repos gitignore package-lock.json)
same_deps() {
  local a=$1 b=$2 q='{dependencies,devDependencies,optionalDependencies,peerDependencies,overrides}'
  if [ -f "$b/package-lock.json" ] && git -C "$b" ls-files --error-unmatch package-lock.json >/dev/null 2>&1; then
    cmp -s "$a/package-lock.json" "$b/package-lock.json"
  else
    [ "$(jq -S "$q" "$a/package.json")" = "$(jq -S "$q" "$b/package.json")" ]
  fi
}

setup_node_deps() {
  local dir=$1 mode=$2 main_nm="$repo_dir/node_modules"
  [ -f "$dir/package.json" ] || { echo "none"; return; }
  case "$dir" in "$wt_root"/*) ;; *) die "refusing to touch deps outside $wt_root: $dir" ;; esac
  if [ -e "$dir/node_modules" ] && [ "$mode" = "auto" ]; then echo "existing"; return; fi
  if [ "$mode" = "auto" ]; then
    if [ -d "$main_nm" ] && same_deps "$repo_dir" "$dir"; then
      if [ -f "$dir/prisma/schema.prisma" ] && ! cmp -s "$repo_dir/prisma/schema.prisma" "$dir/prisma/schema.prisma"; then
        mode="prisma-safe"
      else
        mode="link"
      fi
    else
      mode="install"
    fi
  fi
  rm -rf "$dir/node_modules"
  case "$mode" in
    link)
      ln -s "$main_nm" "$dir/node_modules" ;;
    prisma-safe)
      # per-entry links, but a real copy of the prisma client so generate can't rewrite the user's one
      mkdir "$dir/node_modules"
      for e in "$main_nm"/* "$main_nm"/.[!.]*; do
        [ -e "$e" ] || continue
        n=$(basename "$e")
        case "$n" in
          @prisma|.prisma) cp -R "$e" "$dir/node_modules/$n" ;;
          *) ln -s "$e" "$dir/node_modules/$n" ;;
        esac
      done ;;
    install)
      local log="$dir/.npm-install.log" cmd="install"
      [ -f "$dir/package-lock.json" ] && cmd="ci"
      (cd "$dir" && npm "$cmd" --no-audit --no-fund --loglevel=error >"$log" 2>&1) \
        || { echo "install-failed (npm $cmd, see $log)"; return; }
      mode="install (npm $cmd)" ;;
    none) ;;
    *) die "unknown deps mode: $mode" ;;
  esac
  echo "$mode"
}

# optional per-workspace hook for extra PYTHONPATH entries (e.g. a sibling repo pinned by commit).
# <workspace>/.local/hooks/pythonpath.sh <worktree> prints a colon-separated list, or nothing.
extra_pythonpath() {
  local hook="$ws_root/.local/hooks/pythonpath.sh"
  [ -x "$hook" ] && "$hook" "$1" || true
}

echo "REPO=$slug"
echo "HOST=$host"
echo "WORKTREE=$wt"
echo "HEAD=$head_sha"
echo "BASE=$base $base_sha"
echo "MERGE_BASE=$merge_base"
echo "AHEAD=$ahead BEHIND=$behind"
echo "DIFF=$(git -C "$wt" diff --shortstat "${merge_base}" HEAD | sed 's/^ //')"

if conflicts=$(git -C "$repo_dir" merge-tree --write-tree --name-only "$base_ref" "$pr_ref" 2>/dev/null); then
  echo "MERGE_WITH_BASE=clean"
else
  echo "MERGE_WITH_BASE=conflicts: $(printf '%s\n' "$conflicts" | sed '1d;/^$/,$d' | tr '\n' ' ')"
fi

echo "DEPS=$(setup_node_deps "$wt" "$deps")"
pin=$(extra_pythonpath "$wt")
if [ -d "$repo_dir/.venv" ]; then
  echo "VENV=$repo_dir/.venv/bin/python"
  echo "PYTHONPATH=${pin:+$pin:}$wt"
fi

if [ "$with_base" = 1 ]; then
  bwt="$wt_root/${short}-base-${merge_base:0:7}"
  [ -e "$bwt/.git" ] || git -C "$repo_dir" worktree add --quiet --detach "$bwt" "$merge_base"
  echo "BASE_WORKTREE=$bwt"
  echo "BASE_DEPS=$(setup_node_deps "$bwt" "$deps")"
  bpin=$(extra_pythonpath "$bwt")
  [ -d "$repo_dir/.venv" ] && echo "BASE_PYTHONPATH=${bpin:+$bpin:}$bwt"
fi
exit 0
