#!/usr/bin/env bash
# One command for "is this commit good?": the same checks locally and in CI.
#
#   scripts/check.sh          pre-commit on every file (auto-fixes, then re-checks), then all tests
#   scripts/check.sh tests    tests only (CI runs pre-commit as its own step)
#
# Tests run in every package with a pyproject.toml, on the oldest supported Python (3.11),
# from the lockfile. Prints one summary line per stage; exits non-zero if anything failed.
# The broker's tests need PostgreSQL: set FLEET_TEST_DATABASE_URL (an admin URL), or this script
# starts a throwaway postgres:18 container and removes it afterwards (needs docker).
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
  # Afterwards, take those entries out again (even on Ctrl-C), so the files show as untracked
  # and a later `git commit -a` can't pick them up. Guarded: `git reset` with no paths would
  # unstage everything. --literal-pathspecs: git otherwise reads names as patterns, and an
  # untracked "f[a].py" would also unstage the user's "fa.py".
  ita=$(mktemp)
  undo_ita() {
    [ -s "$ita" ] && xargs -0 git --literal-pathspecs reset -q -- <"$ita"
    rm -f "$ita"
  }
  trap undo_ita EXIT
  git ls-files -z --others --exclude-standard >"$ita"
  [ -s "$ita" ] && xargs -0 git --literal-pathspecs add --intent-to-add -- <"$ita"
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

# The broker's tests run against a real PostgreSQL (ADR 0020), each test in its own database, made
# through FLEET_TEST_DATABASE_URL (an admin URL). CI provides one as a service container. Without it,
# start a throwaway postgres:18 on a random loopback port and remove it afterwards, whatever happens.
# The image digest is the same as compose.yaml's (scripts/tests/test_postgres_pin.py checks).
PG_IMAGE=postgres:18@sha256:c699303802144847d238196173655dbbecf7a96aeca1565f2b02eb671a1f0b6b
pg_container=""
stop_pg() {
  if [ -n "$pg_container" ]; then
    docker rm -f -v "$pg_container" >/dev/null 2>&1
    pg_container=""
  fi
}
if [ -z "${FLEET_TEST_DATABASE_URL:-}" ]; then
  command -v docker >/dev/null 2>&1 || {
    echo "docker is needed for the broker tests (a throwaway postgres), or set FLEET_TEST_DATABASE_URL" >&2
    exit 1
  }
  # EXIT covers normal ends and failures; INT/TERM turn a Ctrl-C into an exit, so EXIT runs too.
  trap stop_pg EXIT
  trap 'exit 130' INT TERM
  # A random password, in this process's environment only (docker reads POSTGRES_PASSWORD by name,
  # so it never appears in a command line); the container is reachable on loopback only, and lives
  # minutes.
  PGPW=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  pg_container="fleet-check-pg-$$-$RANDOM"
  # fsync=off: a database that is thrown away needs no durability, and it makes the tests faster.
  if ! POSTGRES_PASSWORD="$PGPW" docker run -d --name "$pg_container" -e POSTGRES_PASSWORD \
      -p 127.0.0.1::5432 "$PG_IMAGE" -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
      >/dev/null; then
    echo "could not start the throwaway postgres" >&2
    exit 1
  fi
  hostport=""
  for _ in $(seq 1 60); do
    # -h 127.0.0.1: the image's init phase runs a temporary server on the socket only, so the
    # TCP check passes only once the real server is up.
    if docker exec "$pg_container" pg_isready -q -h 127.0.0.1 -U postgres >/dev/null 2>&1; then
      hostport=$(docker port "$pg_container" 5432/tcp | head -n 1)
      break
    fi
    sleep 1
  done
  [ -n "$hostport" ] || { echo "the throwaway postgres did not become ready" >&2; exit 1; }
  FLEET_TEST_DATABASE_URL="postgresql://postgres:${PGPW}@127.0.0.1:${hostport##*:}/postgres"
fi
export FLEET_TEST_DATABASE_URL

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

stop_pg
exit $failed
