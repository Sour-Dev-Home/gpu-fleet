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
- `*.db`, `*.db-wal`, `*.db-shm` and `*.db-journal` are git-ignored, and the broker has no database default: `FLEET_DATABASE_URL` (PostgreSQL, ADR 0020) is required.
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
- [x] A Cloud Run max-instances cap (phase 4 step 4): the bucket is per instance, so the cap bounds the
      fleet-wide rate. Throughput and compute capped; per-request billing of unauthenticated floods mitigated by
      a 4xx alert and, next, the kill switch. In code: `max_instances` (default 1, 3 with volunteers) in
      `infra/gcp/fleet`, checked by a test; not yet applied (ADR 0021, Q65).
- [ ] Unauthenticated request cost: Cloud Run bills every request that reaches the service, including
      the ones the app answers 401 or 429. Max instances bounds throughput and compute only. Decide on a
      budget alert with a kill switch, or a front layer that sheds junk before it is billed.
- [ ] Budget kill switch: built; deployed in phase 4 (ADR 0024). At 100% of $20 a private Cloud Run service drops
      `allUsers` from the broker's invoker role; `terraform apply` restores it. Budget data lags by hours.
- [x] Token rotation: each role accepts a `*_PREVIOUS` token during a rollover; steps in docs/secrets.md.
- [ ] HTTPS only, through Cloud Run's managed TLS. No plain-HTTP listener is exposed.
- [x] Networked store: PostgreSQL (ADR 0020: local Compose and CI; the database has no published port,
      and its URL and password are never logged). Cloud SQL access control (private IP or connector,
      IAM, backups) is checked in phase 4 step 4.
- [x] Store `sha256(lease_token)` instead of the token, so a read of the store can't take over leases.
      Plain SHA-256: tokens are 128 random bits, so there's nothing to brute-force. (Rows from before the
      change were hashed in place on startup, until the PostgreSQL store, which has no old data.)
- Accepted until phase 10: workers share one token, so one token holder (a buggy worker of the
  maintainer's, or a leaked token) can use up the shared poll bucket and slow the others. Heartbeats have
  their own bucket, so a `/lease` hot loop can't cost running jobs their leases; results are
  content-addressed, so the worst case is churn, not loss. Per-worker tokens and buckets fix it (below).
- [x] The broker no longer trusts the `result_key` a worker reports: it recomputes the content address
      from the job's own fields and answers 422 to a mismatch, so a worker can't complete job A with job
      B's result (ADR 0023, Q66). Still open: it does not check that the result is valid (quarantine, below).
- [x] Workers hold no cloud keys and the broker stores none: presigned URLs per object (5 and 10 minutes, only
      for the lease holder's own keys), AWS credentials by web identity federation with a role that trusts
      only the broker's service account and audience, and may only read `inputs/*`/`results/*` and write
      `results/*` (ADR 0025, Q74-Q78). `/complete` HEADs the stored object and refuses one that is missing,
      over the size cap, or whose SHA-256 differs from what `/upload-url` signed, so a result is recorded
      only if it exists in storage. Not yet applied; S3 enforcement of the signed length, checksum and
      no-overwrite is measured by `scripts/probe-presign.py`.
- [ ] Run the security-reviewer again on the deployment (Terraform, IAM, Cloud Run settings).

**Before phase 10 (volunteers):**
- [ ] Per-worker identity: individual hashed tokens, revocation, and every result recording its worker (ADR 0007).
- [ ] Lease caps per worker, and only count attempts from workers in good standing. Otherwise one bad worker can
      lease everything and push every job to `dead` in about `max_attempts × TTL`.
- [ ] Result integrity (the key itself is done, ADR 0023): volunteer results
      go to a quarantine prefix and are promoted only after a check or after a second worker agrees.
