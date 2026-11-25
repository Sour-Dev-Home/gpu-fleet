#!/usr/bin/env bash
# Push the current branch, open its PR, and see it through to main.
#
#   scripts/ship-pr.sh "<title>" "<body>"            auto-merge (squash) once CI passes, then sync main
#   scripts/ship-pr.sh --hold "<title>" "<body>"     open the PR and wait for CI, but don't merge:
#                                                    for changes the maintainer must approve first
#
# Commit first: this never commits. The body should end with the attribution line.
# Exits non-zero if CI fails or the PR doesn't merge in time.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

hold=0
if [ "${1:-}" = --hold ]; then hold=1; shift; fi
title=${1:?title required}
body=${2:?body required}

branch=$(git branch --show-current)
[ "$branch" != main ] || { echo "on main: create a branch first" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "uncommitted changes" >&2; exit 1; }

git push -q -u origin HEAD
# Only an OPEN PR for this branch counts: a reused branch name may have an old merged one.
pr=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // empty')
if [ -z "$pr" ]; then
  gh pr create --title "$title" --body "$body" >/dev/null
  pr=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number')
fi
echo "PR #$pr: $(gh pr view "$pr" --json url --jq .url)"

if [ $hold -eq 0 ]; then
  # Right after a push GitHub may still be updating the PR ("Base branch was modified").
  # That's transient: retry a few times before giving up.
  for attempt in 1 2 3 4 5; do
    if err=$(gh pr merge "$pr" --auto --squash 2>&1); then break; fi
    [ $attempt -lt 5 ] || { echo "could not enable auto-merge: $err" >&2; exit 1; }
    sleep $((attempt * 3))
  done
fi

# Checks can take a few seconds to register after a push.
for _ in $(seq 1 12); do
  out=$(gh pr checks "$pr" 2>&1) || true
  case "$out" in *"no checks reported"*) sleep 5 ;; *) break ;; esac
done
if ! gh pr checks "$pr" --watch --fail-fast --interval 15 >/dev/null; then
  gh pr checks "$pr" | grep -v pass || true
  echo "PR #$pr: CI FAILED" >&2
  exit 1
fi
echo "PR #$pr: CI passed"

if [ $hold -eq 1 ]; then
  echo "PR #$pr: held for approval (not merged)"
  exit 0
fi

# Auto-merge fires a few seconds after the last check passes.
for _ in $(seq 1 24); do
  state=$(gh pr view "$pr" --json state --jq .state)
  [ "$state" = MERGED ] && break
  sleep 5
done
[ "$state" = MERGED ] || { echo "PR #$pr: not merged (state $state)" >&2; exit 1; }

git switch -q main
git pull -q --ff-only
echo "PR #$pr: merged; main at $(git log --oneline -1)"
