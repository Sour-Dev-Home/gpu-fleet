# 0009. CI supply chain: SHA-pinned actions, Dependabot cooldown

**Status:** Accepted
**Date:** 2026-11-25

## Context
The repo is public, and PRs auto-merge once CI passes. Version tags on GitHub Actions can be moved, and a moved
tag runs new code in CI (as in the 2025 tj-actions/changed-files compromise). A freshly published malicious
package release can be auto-merged within hours.

## Decision
- Every action is pinned to a full commit SHA with a `# vX.Y.Z` comment. Trivy and hadolint run through pinned
  actions, not `:latest` images.
- Tool versions in CI are pinned (`pre-commit`, `uv`). Python packages install with `uv sync --locked`.
- Checkout uses `persist-credentials: false`, so the job token isn't on disk while PR code runs.
- Dependabot groups updates per ecosystem. Python updates wait out a 7-day `cooldown`.
- An ecosystem is only added to Dependabot once its manifest exists (it fails on empty directories).
- Each package is tested on the oldest supported Python (3.11).

## Consequences
- Major-version bumps from Dependabot need a human review before merging.
- The infra-reviewer agent checks every change under `.github/`.
