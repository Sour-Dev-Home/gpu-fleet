# Security: threat model and backlog

Reviewed by the security-reviewer agent on 2026-11-25 (first pass: broker API, store, worker client). Re-run it
before each gate below, and whenever auth, the protocol or exposure changes.

## Threat model by phase

| Phase | Exposure | Who holds a token | Main threats |
|---|---|---|---|
| 0-3 | Broker on localhost | The maintainer only | Committed secrets or DB files; a leaked token |
| 4-9 | Broker public on Cloud Run | The maintainer's own backends | Anyone on the internet: token guessing, oversized or malformed requests, cost abuse, API discovery |
| 10 | Public, with volunteers | Untrusted people | A malicious worker: draining or killing the queue, poisoned results, getting footage jobs |

Out of scope everywhere: an attacker on the maintainer's own PC (they would already have the vault).

## Done (security pass 1, ADR 0011)
- `/jobs` needs a separate **admin token**; worker tokens can't add jobs. Tokens must be at least 32 characters
  and must differ from each other.
- Token comparison uses bytes in constant time, so a non-ASCII header gets 401, not 500.
- Request fields have format and length limits (hashes are `^[0-9a-f]{64}$`), unknown fields are rejected, and
  declared bodies over 16 KiB get 413.
- `/docs` and `/openapi.json` are off unless `FLEET_DOCS=1`.
- The worker refuses to send its token over `http://` to anything but loopback.
- `*.db`, `*.db-wal`, `*.db-shm` and `*.db-journal` are git-ignored, and the database defaults to `data/broker.db`.
- Baseline: no committed secrets or personal paths; pip-audit is clean on both lockfiles; SQL is
  parameterized; lease tokens come from `secrets`; tokens never appear in logs or exceptions.

## Backlog, gated by phase

**Before phase 3 (schema design):**
- [ ] Add a `job_class` column (for example `footage`, `text`, `open`) and give each worker token an allowed-classes
      scope, filtered in the claim's `WHERE`. Copyrighted footage must never be leased to a volunteer (ADR 0002).

**Before phase 4 (public broker):**
- [ ] Rate limits per token on `/lease` and `/heartbeat`, plus a Cloud Run max-instances cap (cost and thread
      pool protection). Cloud Armor or an API gateway body limit as well, since chunked bodies skip the
      Content-Length check.
- [ ] Token rotation: accept two tokens during a rollover, and document the steps in docs/secrets.md.
- [ ] HTTPS only, through Cloud Run's managed TLS. No plain-HTTP listener is exposed.
- [ ] Replace SQLite with a networked store (ADR 0005), then re-check its access control.
- [ ] Store `sha256(lease_token)` instead of the token, so a read of the store can't take over leases.
- [ ] Run the security-reviewer again on the deployment (Terraform, IAM, Cloud Run settings).

**Before phase 10 (volunteers):**
- [ ] Per-worker identity: individual hashed tokens, revocation, and every result recording its worker (ADR 0007).
- [ ] Lease caps per worker, and only count attempts from workers in good standing. Otherwise one bad worker can
      lease everything and push every job to `dead` in about `max_attempts × TTL`.
- [ ] Result integrity: the broker computes `result_key` itself instead of trusting the client. Volunteer results
      go to a quarantine prefix and are promoted only after a check or after a second worker agrees.
