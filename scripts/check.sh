#!/usr/bin/env bash
# One command for "is this commit good?": the same checks locally and in CI.
#
#   scripts/check.sh          pre-commit on every file (auto-fixes, then re-checks), then all tests
#   scripts/check.sh tests    tests only (CI runs pre-commit as its own step)
#
# Tests run in every package with a pyproject.toml, on the oldest supported Python (3.11),
# from the lockfile. Prints one summary line per stage; exits non-zero if anything failed.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

PY=3.11
failed=0
mode=${1:-all}

if [ "$mode" = all ]; then
  # --all-files means all TRACKED files: a new file nobody has `git add`ed yet would be
  # skipped and reported "ok". Mark untracked (non-ignored) files intent-to-add first, so
  # pre-commit sees them; this stages no content. NUL-separated (-z / -0) for any file name,
  # and portable (BSD xargs has no -d).
  ita=$(mktemp)
  git ls-files -z --others --exclude-standard >"$ita"
  # Afterwards, take those entries out again (even on Ctrl-C), so the files show as untracked
  # and a later `git commit -a` can't pick them up. Guarded: `git reset` with no paths would
  # unstage everything.
  undo_ita() {
    [ -s "$ita" ] && xargs -0 git reset -q -- <"$ita"
    rm -f "$ita"
  }
  trap undo_ita EXIT
  [ -s "$ita" ] && xargs -0 git add --intent-to-add -- <"$ita"
  # Hooks like ruff --fix and end-of-file-fixer change files and report "Failed" once.
  # Re-run: a second failure is a real problem.
  if pre-commit run --all-files >/dev/null 2>&1 || pre-commit run --all-files; then
    echo "pre-commit: ok"
  else
    echo "pre-commit: FAILED"
    failed=1
  fi
  undo_ita
  trap - EXIT
fi

pkgs=$(git ls-files '*pyproject.toml')
[ -n "$pkgs" ] || { echo "no pyproject.toml found: nothing tested" >&2; exit 1; }
for p in $pkgs; do
  d=$(dirname "$p")
  out=$(cd "$d" && uv sync -q --locked --python "$PY" && uv run -q --locked --python "$PY" pytest -q -p no:cacheprovider 2>&1)
  status=$?
  summary=$(printf '%s\n' "$out" | tail -n 1)
  if [ $status -eq 0 ]; then
    echo "$d: $summary"
  else
    printf '%s\n' "$out" | grep -E "^(FAILED|ERROR)|^E  " | head -n 20
    echo "$d: FAILED ($summary)"
    failed=1
  fi
done

# Tests for the repo's own tooling (scripts/tests): stdlib + pytest, pinned to the packages' locked version.
if [ -d scripts/tests ]; then
  out=$(uv run -q --no-project --python "$PY" --with pytest==9.2.0 pytest -q -p no:cacheprovider scripts/tests 2>&1)
  status=$?
  if [ $status -eq 0 ]; then echo "scripts: $(printf '%s\n' "$out" | tail -n 1)"; else
    printf '%s\n' "$out" | tail -n 20; echo "scripts: FAILED"; failed=1; fi
fi

exit $failed
