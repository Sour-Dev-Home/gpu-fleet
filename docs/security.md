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
- [x] Data class on every job, a clearance per worker token, capabilities declared at lease time, all filtered in
      the atomic claim. [ADR 0015](adr/0015-data-class-clearance-capabilities.md), built 2026-11-25, each
      condition mutation-checked. Copyrighted footage must never be leased to a volunteer (ADR 0002).

**Before phase 4 (public broker):**
- [x] Rate limits on the worker endpoints (`/lease`, `/heartbeat`, `/complete`): a token bucket per role,
      20/s with bursts of 100, after authentication so strangers can't use it up; 429 with Retry-After,
      which workers retry like a 503; counted as `fleet.http.limited`. Built 2026-11-25 (ADR 0019 step 1).
- [x] Body limit for chunked requests: counted as the body arrives (`bodylimit.py`), 413 past 16 KiB.
      In the app rather than Cloud Armor, which needs a load balancer (about $18/month,
      docs/research/phase-4.md). Q46, 2026-11-25.
- [ ] A Cloud Run max-instances cap (phase 4 step 4): the bucket is per instance, so the cap bounds the
      fleet-wide rate and the bill.
- [x] Token rotation: each role accepts a `*_PREVIOUS` token during a rollover; steps in docs/secrets.md.
- [ ] HTTPS only, through Cloud Run's managed TLS. No plain-HTTP listener is exposed.
- [ ] Replace SQLite with a networked store (ADR 0005), then re-check its access control.
- [x] Store `sha256(lease_token)` instead of the token, so a read of the store can't take over leases.
      Plain SHA-256: tokens are 128 random bits, so there's nothing to brute-force. Rows from before the
      change are hashed in place on startup.
- Accepted until phase 10: workers share one token, so one token holder (a buggy worker of the
  maintainer's, or a leaked token) can use up the shared poll bucket and slow the others. Heartbeats have
  their own bucket, so a `/lease` hot loop can't cost running jobs their leases; results are
  content-addressed, so the worst case is churn, not loss. Per-worker tokens and buckets fix it (below).
- [ ] Run the security-reviewer again on the deployment (Terraform, IAM, Cloud Run settings).

**Before phase 10 (volunteers):**
- [ ] Per-worker identity: individual hashed tokens, revocation, and every result recording its worker (ADR 0007).
- [ ] Lease caps per worker, and only count attempts from workers in good standing. Otherwise one bad worker can
      lease everything and push every job to `dead` in about `max_attempts × TTL`.
- [ ] Result integrity: the broker computes `result_key` itself instead of trusting the client. Volunteer results
      go to a quarantine prefix and are promoted only after a check or after a second worker agrees.
