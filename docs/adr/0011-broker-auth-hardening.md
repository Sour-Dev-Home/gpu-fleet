# 0011. Broker auth hardening: admin and worker tokens, strict inputs, no public docs

**Status:** Accepted (maintainer, 2026-11-25; from security pass 1)
**Date:** 2026-11-25
**Amends:** [0007](0007-http-contract-and-auth.md) (one shared token becomes two role tokens)

## Context
The first security review ([docs/security.md](../security.md)) found nothing exploitable on localhost, but several
defaults that would be unsafe once the broker is public in phase 4. The most serious: any worker token could add
jobs. That would let it flood the queue, or reserve a `job_id` so that `INSERT OR IGNORE` silently drops the real job.

## Decision
- **Two roles, two tokens:** `FLEET_WORKER_TOKEN` for `/lease`, `/heartbeat` and `/complete`, and
  `FLEET_ADMIN_TOKEN` for `/jobs`. Both come from the `fleet` vault. The broker refuses to start if either is
  missing, shorter than 32 characters, or the two are the same.
- Tokens are compared as bytes with `hmac.compare_digest`, so a malformed header gets 401, never 500.
- **Strict request models:** hashes must be lowercase 64-character hex; names are limited to 200 characters from
  a safe character set; unknown fields are rejected (422); declared bodies over 16 KiB are rejected (413).
- **No API description in production:** `/docs` and `/openapi.json` exist only when `FLEET_DOCS=1`.
- **Workers refuse cleartext:** `HttpBroker` won't send its token over `http://` unless the host is loopback, or a
  test passes `allow_http=True`.

## Consequences
- Running the broker locally now needs both secrets:
  `with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- …`.
- Lowercase-only hashes mean one input has exactly one spelling, which keeps content addressing consistent
  ([0003](0003-content-addressed-results.md)).
- Still one shared worker token: per-worker identity, lease caps and result integrity are backlog items gated
  on phase 10 (docs/security.md).

## Alternatives considered
- **Admin route on a separate service or IAM-only:** cleaner, but it means more infrastructure before there is
  any infrastructure. Revisit in phase 4 (e.g. `/jobs` reachable only through IAM-authenticated Cloud Run).
- **Leave it for phase 4:** cheap to fix now, and easy to forget under deployment pressure.
