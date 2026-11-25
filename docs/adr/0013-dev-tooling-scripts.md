# 0013. Repo scripts for checking, shipping and mutation testing

**Status:** Accepted (maintainer, 2026-11-25: "implement all" after the session retro)
**Date:** 2026-11-25

## Context
A retro of phases 0a to 2a found that the costliest mistakes were in the glue between commands, not in the
checks: CI-polling loops rewritten for every PR (one hung on a short commit ID, one raced auto-merge), pre-commit
reporting lint errors it could have fixed, and a mutant that "died" of a NameError being counted as caught.

## Decision
- **`scripts/check.sh`** is the one "is this good?" command: pre-commit on every file, then every package's tests on
  Python 3.11 from the lockfile, plus the tooling's own tests. CI runs `scripts/check.sh tests` after its
  pre-commit step, so local and CI can't drift apart.
- **`scripts/ship-pr.sh`** pushes, opens the PR, enables auto-merge (retrying GitHub's transient "base branch was
  modified"), waits on `gh pr checks --watch`, and syncs main. `--hold` stops before merging, for changes awaiting
  the maintainer's approval.
- **`scripts/mutate.py`** applies one exact replacement, runs the tests, always restores the file, and grades the
  mutant CAUGHT, SURVIVED or INVALID (killed by NameError, SyntaxError or an import or collection error).
- The ruff hook runs with `--fix`; `.gitattributes` stores and checks out text as LF.

## Consequences
- Repeated procedures live in tested files, not in each session's memory.
- `mutate.py` uses only the standard library, so it runs with `uv run --no-project` from any package.
- The scripts are Bash: run them from Git Bash or WSL on Windows (CI is Linux).

## Alternatives considered
- **mutmut or cosmic-ray:** automatic mutant generation, but slow on a whole package and noisy. Hand-picked
  mutants against specific behaviours teach more and run in seconds.
- **A Makefile or justfile:** one more tool to install on Windows for three commands.
