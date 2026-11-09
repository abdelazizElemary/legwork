#!/usr/bin/env bash
# Make a fresh branch for a ticket from the latest default branch, in its own worktree.
# The user's checkout and its branches are never touched.
#
# usage: ticket_worktree.sh <repo-dir> <KEY-123> <fix|feat|chore|docs|refactor|test|perf> <slug> [--base <branch>]
#          [--branch-format '{type}/{key}-{slug}']   ({key} is lowercased)  [--round N]
#   --base B   start from origin/B instead of the default branch (a follow-up stacked on this ticket's open PR)
#   --round N  a follow-up round: its own worktree, so the earlier round's worktree stays as it is
# prints WORKTREE, BRANCH, BASE, AUTHOR, DEPS and, for Python repos, VENV and PYTHONPATH
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[ $# -ge 4 ] || die "usage: ticket_worktree.sh <repo-dir> <KEY-123> <type> <slug> [--base B] [--branch-format F] [--round N]"
repo_dir=$(cd "$1" && pwd) || die "no such dir: $1"
key=$2; type=$3; slug=$4; base=""; branch_format='{type}/{key}-{slug}'; round=1
shift 4
while [ $# -gt 0 ]; do
  case "$1" in
    --base) [ $# -ge 2 ] || die "--base needs a branch"; base=$2; shift 2 ;;
    --branch-format) [ $# -ge 2 ] || die "--branch-format needs a format"; branch_format=$2; shift 2 ;;
    --round) [ $# -ge 2 ] || die "--round needs a number"; round=$2; shift 2 ;;
    *) die "unknown flag: $1" ;;
  esac
done
[[ "$round" =~ ^[0-9]+$ ]] || die "round must be a number: $round"
[ -z "$base" ] || [[ "$base" =~ ^[A-Za-z0-9._/-]+$ ]] || die "bad base branch: $base"
[[ "$branch_format" == *"{key}"* && "$branch_format" == *"{slug}"* ]] || die "branch format needs {key} and {slug}: $branch_format"
[[ "$key" =~ ^[A-Z][A-Z0-9]*-[0-9]+$ ]] || die "ticket must look like ABC-123: $key"
[[ "$type" =~ ^(fix|feat|chore|docs|refactor|test|perf)$ ]] || die "type must be fix, feat, chore, docs, refactor, test or perf"
[[ "$slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] && [ ${#slug} -le 40 ] || die "slug must be short kebab-case: $slug"
git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1 || die "not a git repo: $repo_dir"

key_lower=$(printf '%s' "$key" | tr 'A-Z' 'a-z')
branch=${branch_format//\{type\}/$type}
branch=${branch//\{key\}/$key_lower}
branch=${branch//\{slug\}/$slug}
git check-ref-format --branch "$branch" >/dev/null 2>&1 || die "not a valid branch name: $branch"
repo_name=$(basename "$repo_dir")
ws_root=$(dirname "$repo_dir")
if [ "$(basename "$ws_root")" = "clones" ] && [ "$(basename "$(dirname "$ws_root")")" = ".local" ]; then
  ws_root=$(dirname "$(dirname "$ws_root")")
fi
wt_root="$ws_root/.local/wt"
wt="$wt_root/${repo_name}-${key_lower}"
[ "$round" -gt 1 ] && wt="${wt}-r${round}"
mkdir -p "$wt_root"

if [ -z "$base" ]; then
  base=$(git -C "$repo_dir" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##') || true
  [ -n "$base" ] || die "origin/HEAD isn't set in $repo_dir, pass --base <branch> (or run: git -C $repo_dir remote set-head origin --auto)"
fi
git -C "$repo_dir" fetch --quiet origin "+refs/heads/${base}:refs/remotes/origin/${base}"

if [ -e "$wt/.git" ]; then
  # resuming: the worktree must already be this ticket's branch
  current=$(git -C "$wt" branch --show-current)
  [ "$current" = "$branch" ] || die "$wt is on '$current', not '$branch'"
else
  git -C "$repo_dir" show-ref --verify --quiet "refs/heads/$branch" && die "branch $branch already exists locally, pick another slug"
  if git -C "$repo_dir" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    die "branch $branch already exists on the remote, pick another slug"
  fi
  git -C "$repo_dir" worktree add --quiet -b "$branch" "$wt" "refs/remotes/origin/${base}"
fi

setup_node_deps() {
  local dir=$1 main_nm="$repo_dir/node_modules"
  [ -f "$dir/package.json" ] || { echo "none"; return; }
  [ -e "$dir/node_modules" ] && { echo "existing"; return; }
  local same=1
  if git -C "$dir" ls-files --error-unmatch package-lock.json >/dev/null 2>&1; then
    cmp -s "$repo_dir/package-lock.json" "$dir/package-lock.json" || same=0
  else
    local q='{dependencies,devDependencies,optionalDependencies,peerDependencies,overrides}'
    [ "$(jq -S "$q" "$repo_dir/package.json")" = "$(jq -S "$q" "$dir/package.json")" ] || same=0
  fi
  if [ -d "$main_nm" ] && [ $same = 1 ]; then
    ln -s "$main_nm" "$dir/node_modules"; echo "link"
  else
    local cmd=install; [ -f "$dir/package-lock.json" ] && cmd=ci
    (cd "$dir" && npm "$cmd" --no-audit --no-fund --loglevel=error >"$dir/.npm-install.log" 2>&1) \
      && echo "install (npm $cmd)" || echo "install-failed (see $dir/.npm-install.log)"
  fi
}

# optional per-workspace hook for extra PYTHONPATH entries, same as pr_worktree.sh
hook="$ws_root/.local/hooks/pythonpath.sh"
pin=""
[ -x "$hook" ] && pin=$("$hook" "$wt" || true)

echo "WORKTREE=$wt"
echo "BRANCH=$branch"
echo "BASE=$base $(git -C "$repo_dir" rev-parse "refs/remotes/origin/${base}")"
echo "AUTHOR=$(git -C "$wt" config user.name) <$(git -C "$wt" config user.email)>"
echo "DEPS=$(setup_node_deps "$wt")"
if [ -d "$repo_dir/.venv" ]; then
  echo "VENV=$repo_dir/.venv/bin/python"
  echo "PYTHONPATH=${pin:+$pin:}$wt"
fi
