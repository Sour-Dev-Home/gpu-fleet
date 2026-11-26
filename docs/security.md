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
- The clients (the worker and `fleet-enqueue`) refuse to send a token over `http://` to anything but loopback
  (unless explicitly opted in), and never follow redirects, so a plain-HTTP request to the public broker (which
  Cloud Run answers with a 302 to https) never carries a token.
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
      a 4xx alert and the kill switch. `max_instances` (default 1, 3 with volunteers) in `infra/gcp/fleet`, set
      on the revision and, since Q95, on the service too (`run.googleapis.com/maxScale`), checked by a test;
      applied (ADR 0021, Q65).
- [x] Unauthenticated request cost: Cloud Run bills every request that reaches the service, including
      the ones the app answers 401 or 429. Max instances bounds throughput and compute only. Answer: a flood
      alert (more than 5 client errors/s for 5 minutes) trips the kill switch within minutes (Q93), and the budget
      switch (below) is the slow backstop.
- [x] Budget kill switch: applied (ADR 0024). At 100% of $20 a private Cloud Run service drops `allUsers` from the
      broker's invoker role; `terraform apply` restores it. Budget data lags by hours, hence the flood alert path:
      an open incident of the 4xx policy, on its own Pub/Sub topic and `/alert` path with a strict parser, does
      the same (Q93; the Monitoring service agent name is unverified until the first apply).
- [x] Token rotation: each role accepts a `*_PREVIOUS` token during a rollover; steps in docs/secrets.md.
- [x] HTTPS only, through Cloud Run's managed TLS. Plain `http://` is not refused: Cloud Run answers it with a
      302 redirect to `https://`. That is safe because no client sends a token over plain HTTP to a non-loopback
      host (they refuse) and none follows a redirect; a browser or a scanner just ends up on https.
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
      only the broker's service account and audience, and may only read `inputs/open/*`/`results/open/*` and write
      `results/open/*`, the data class being in the key (ADR 0025, Q74-Q78, Q82). `/complete` HEADs the stored object and refuses one that is missing,
      over the size cap, or whose SHA-256 differs from what `/upload-url` signed, so a result is recorded
      only if it exists in storage. Applied (S3 mode); S3 enforcement of the signed length, checksum and
      no-overwrite is measured by `scripts/probe-presign.py`.
- [x] Data-plane lock on the results bucket (Q94): the bucket policy denies `s3:GetObject`, `PutObject` and
      `DeleteObject` on `inputs/*` and `results/*` to every principal except the broker role and the maintainer's
      admin principal (`admin_principal_arn`, by `aws:PrincipalArn`), so a leaked or over-broad identity in the account cannot
      reach the data. The TLS-only denies stay. Object actions only, so Terraform cannot lock itself out.
- [x] Security review of the deployment (Terraform, IAM, Cloud Run settings), 2026-11-26: Q93-Q95. Re-run on the next
      change to IAM, networking or exposure.
- [x] `pip-audit` over the three uv lockfiles (broker, worker, kill switch) runs in CI.
- [x] `fleet-enqueue --data-class` is required, with no default, so a forgotten flag cannot publish footage as `open`.

## Accepted trade-offs (decided 2026-11-26, Q95)
- **Request logs below 500 are excluded** from Cloud Logging (privacy over forensics: a 401 or 404 line would carry
  caller addresses and paths). The request-count metrics remain, which is what the 4xx alert and the dashboards
  use; only the per-request detail of a 2xx-4xx is gone, so after a flood you can see how much, not from whom.
- **The broker role's `s3:ListBucket` exposes hashed key names** to the broker (and to anyone who gets its
  credentials). It is needed for a real 404 on HEAD: without it S3 answers 403 for a missing key, which the broker
  must treat as a fault. The names are content hashes under `inputs/` and `results/`, which the role can already
  read for the open classes, and the data-plane lock above keeps anyone else out.
- **The flood alert trades availability for a hard cost cap.** About 1,500 unauthenticated 4xx requests other
  than 429 in 5 minutes (5/s sustained) open the incident, the kill switch removes `allUsers` from the broker,
  and the broker stays offline until a reviewed `terraform apply`. Anyone who can send that many bad requests can
  therefore take the public broker down. 429 is excluded (Cloud Run's no-free-instance and the broker's rate
  limit are load shedding, not a flood). Revisit with Cloud Armor or rate limiting at the edge once volunteers
  depend on the broker (phase 5+).

**Before phase 10 (volunteers):**
- [ ] Per-worker identity: individual hashed tokens, revocation, and every result recording its worker (ADR 0007).
- [ ] Lease caps per worker, and only count attempts from workers in good standing. Otherwise one bad worker can
      lease everything and push every job to `dead` in about `max_attempts × TTL`.
- [ ] Result integrity (the key itself is done, ADR 0023): volunteer results
      go to a quarantine prefix and are promoted only after a check or after a second worker agrees.
- [ ] First-stored-wins makes a poisoned result permanent (ADR 0025): the first object stored under a result key
      is adopted by every later attempt. Needs an admin path to quarantine or delete a result object and reset
      its job, and a record of which worker token or lease wrote each result.
