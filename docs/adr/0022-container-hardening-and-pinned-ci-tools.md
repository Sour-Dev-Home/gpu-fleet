# 0022. Container hardening, pinned CI tools and the enqueue transport guard

**Status:** Accepted (maintainer, 2026-11-25, Q67)
**Date:** 2026-11-25
**Builds on:** [0009](0009-ci-supply-chain.md), [0014](0014-container-images-and-compose.md),
[0019](0019-public-broker-on-cloud-run.md).

## Context
A security review of phase 4 step 2 found three kinds of gap. A compromised broker or worker process could
write anywhere in its container and keep Linux capabilities it never needs. CI installed pre-commit and uv by
version only, and pre-commit hooks by movable tags, so a hijacked tag or package would run in CI with the
repository checked out. And `fleet-enqueue` sent the admin token over any scheme, followed redirects with the
header, and could print the token in a traceback.

## Decision
- **Compose containers:** broker and worker run `read_only: true`, `cap_drop: [ALL]` and
  `security_opt: ["no-new-privileges:true"]`, with `/tmp` as a size-limited tmpfs
  (`size=64m,mode=1777,noexec,nosuid,nodev`). The worker's results volume stays writable. Postgres gets only
  `no-new-privileges`: its image starts as root and drops privileges itself, so `cap_drop: ALL` breaks it.
  Verified in a throwaway Compose project: both services start and run jobs end to end.
- **Pre-commit hooks** are pinned to full commit SHAs (`# frozen: vX` names the tag). Dependabot's
  `pre-commit` ecosystem proposes bumps.
- **CI tools:** pre-commit and uv install with `pip install --require-hashes` from
  `.github/ci/requirements.txt`, compiled by `uv pip compile --generate-hashes` (all transitive packages
  hashed). Dependabot's `pip` ecosystem updates it; uv is ignored there and bumped by hand with the Dockerfiles.
  Actions stay SHA-pinned (ADR 0009).
- **`fleet-enqueue`:** refuses a non-https URL unless the host is loopback (any spelling) or the caller set
  `FLEET_ALLOW_HTTP=1` and the host is a single-label name such as `broker` (Compose's service name). URLs with
  userinfo, a backslash or a %-escape are refused, because urllib and `urlsplit` disagree on their host.
  Redirects are never followed (a 3xx is an error). The token is stripped of surrounding whitespace, rejected
  if it holds control characters, and no error path prints it.

## Consequences
- A write outside `/tmp` and the results volume fails in the broker and worker containers; anything new that
  needs disk must declare a volume or tmpfs.
- `noexec` on `/tmp` means nothing can be run from it. Nothing here does.
- Hash-pinned CI installs fail loudly when a package changes; the cost is regenerating the file for each bump.
- Plain-http enqueue to a dotted hostname needs an https endpoint or a tunnel to loopback.

## Alternatives considered
- `cap_drop: ALL` on postgres: breaks the entrypoint's user switch. Rejected.
- Pinning only the top-level CI packages: leaves the transitive ones unpinned. Rejected.
- Letting `FLEET_ALLOW_HTTP` allow any host: the opt-in would reach the internet from inside the broker container.
  Rejected for single-label names only.
