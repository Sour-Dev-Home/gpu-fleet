# 0020. PostgreSQL job store: pool, claim, migrations and 503 on connection errors

**Status:** Accepted (maintainer, 2026-11-25, Q44-Q55)
**Date:** 2026-11-25
**Builds on:** [0012](0012-context-hash-in-result-key.md), [0015](0015-data-class-clearance-capabilities.md),
[0019](0019-public-broker-on-cloud-run.md). **Supersedes:** [0005](0005-sqlite-job-store.md), and the
SQLite-specific parts of [0016](0016-sqlite-busy-handling.md).

## Context
Cloud Run runs several short-lived instances with no shared disk, so SQLite (ADR 0005) cannot be the store once
the broker is public (ADR 0019). The decision to use Cloud SQL for PostgreSQL, **everywhere** (Compose and tests
too), was made with ADR 0019. This ADR is the store itself. Cloud SQL, Terraform and its access control come in
step 4; this step is local and CI only.

Napkin math: the rate limit allows 20 requests/s per instance, and a store call is about 3 ms of SQL. Little's
law (connections in use = arrival rate x time held): 20 x 0.003 = **0.06 connections busy**. Four per instance is
more than 60 times the need; the pool is there for bursts and for a slow query, not for throughput.

## Decision
- **Driver and pool:** psycopg 3 (`psycopg[binary]`) with `psycopg_pool.ConnectionPool`: `min_size=0` (nothing
  held while the broker is idle or scaled to zero), `max_size=4`, 2 s checkout timeout, `max_lifetime=1800`,
  connections checked on checkout (`check_connection`, so a connection the server dropped is replaced, not handed
  out), autocommit. One pool per app, opened at startup, closed in the FastAPI lifespan (so SIGTERM during a
  rollout ends the connections cleanly). A connection is held only while SQL runs, never across other work.
- **Instance cap:** `max_instances (3) x pool (4) = 12` connections at most. That must stay below Cloud SQL's
  limit for the smallest tier (**UNVERIFIED, about 25: check it in step 4**). Past 6 instances (6 x 4 = 24) the
  rule fails; see the rejected pooler below.
- **Session timeouts**, set on every pooled connection by the pool's `configure` callback: `statement_timeout=5s`,
  `idle_in_transaction_session_timeout=10s`, `lock_timeout=3s`. A stuck query or transaction can't hold one of
  four connections for long.
- **Store:** `JobStore` keeps its public methods, return types and semantics (clearance/capability/model filter,
  lazy expiry, dead-lettering at `max_attempts`, sha256-hashed lease tokens, idempotent `complete` with `first`).
  Postgres has no rowid, so an identity column `seq` gives FIFO order. Times are `DOUBLE PRECISION` (a 4-byte
  `REAL` would round epoch seconds to about 2 minutes).
- **The claim is one statement:** a CTE selects the oldest matching available job `FOR UPDATE SKIP LOCKED`, and
  an `UPDATE ... RETURNING` leases it. Concurrent claims skip rows another claim holds instead of waiting, so
  workers never queue behind each other and never take the same job. The claim also refuses a job with
  `attempts >= max_attempts` itself, so dead-lettering needs no atomicity with it.
- **Index:** `jobs (seq) WHERE state IN ('queued','leased')` (partial). Done and dead jobs, the bulk of a
  long-lived table, are not in it; the planner can walk it in FIFO order without a sort. A test runs `EXPLAIN`
  with sequential scans, bitmap scans and sorts disabled and requires this index. (At a few hundred rows
  Postgres rightly prefers a sort of the few open rows; the test asks whether the index *can* serve the claim.)
- **Migrations** (create the table and index, `ADD COLUMN IF NOT EXISTS`) run at startup in one transaction under
  `pg_advisory_xact_lock`, with `lock_timeout`, so instances starting together (a rollout, a scale-out) take turns
  and never race on `CREATE` or `ALTER`. ADR 0012's rule stays: expand only, nullable columns, never rename or
  drop. When a migration needs a backfill, it moves to a Cloud Run Job run before the rollout, not startup code.
  The SQLite-only pieces are gone: WAL, `busy_timeout`, and hashing old plaintext tokens (no old Postgres data).
- **Errors (ADR 0016's contract, new causes):** no connection (refused, dropped, unknown database), pool checkout
  timeout, statement timeout, lock timeout, a restarting or full server -> `503` with `Retry-After: 1`, a warning
  log naming only the error class and SQLSTATE, and the `fleet.http.busy` counter. Anything else (a missing
  table, a bug) stays a 500. Workers and `fleet-enqueue` already retry 503 with backoff.
- **Config:** `FLEET_DATABASE_URL` is required: the broker refuses to start without it, like the tokens. It holds
  the password, so it appears in no log line or exception message (an unparseable URL is reported without being
  quoted).
- **Compose and tests:** a `postgres:18` service pinned by digest (18 because the Cloud SQL console defaults to it: local, CI and Cloud
  SQL run the same major version; the same digest in Compose, CI and `scripts/check.sh`, checked by a test), a named volume, a `pg_isready` healthcheck and
  no host port; its password is `FLEET_DB_PASSWORD` from the vault. Tests run against real Postgres, each in its
  own database (`FLEET_TEST_DATABASE_URL` admin URL); `scripts/check.sh` starts a throwaway container, and CI has a
  service container.
- **Maintenance window:** Sunday 04:00, set in step 4 with the instance. Until then, nothing to configure.

## Consequences
- One code path from a laptop to production: SKIP LOCKED, timeouts and pool behaviour are tested, not assumed.
- The job store needs a running Postgres: `docker compose up` starts one; `scripts/check.sh` needs Docker (or a
  `FLEET_TEST_DATABASE_URL`). The local suite takes longer (about 45 s for the broker, mostly a few 3-5 s
  timeout tests and per-test database creation).
- After a database outage the pool backs off between reconnects, so the first requests after recovery can still be
  503 for a second or two. That is what `Retry-After` is for.
- A failed migration stops the broker at startup (it does not serve with a half-made schema). The advisory lock is
  transaction-scoped, so a crashed migrator cannot leave it held.
- Backups, high availability, IAM auth and the connector are step 4's decisions.
- Local SQLite jobs are not migrated. The old `broker-data` volume is orphaned: re-enqueue what you need, and
  remove it with `docker volume rm gpu-fleet_broker-data` when ready.
- Dependabot's docker-compose ecosystem bumps only `compose.yaml`'s postgres digest. `ci.yml` and `check.sh` must be
  updated in the same PR; `test_postgres_pin.py` fails until they are.
- `psycopg-binary` bundles libpq and OpenSSL: their fixes arrive only with a new wheel, delayed by the 7-day
  Dependabot cooldown. Accepted for now.

## Alternatives considered
- **Neon (serverless Postgres):** free tier and scale-to-zero suit a hobby project, but a cold start adds
  roughly 0.5-1 s to the first request (UNVERIFIED), a third vendor holds the job data, and Cloud SQL sits next to
  Cloud Run on the credits already in hand.
- **Firestore:** serverless and free at this scale (a few thousand jobs is far inside the daily free quota,
  UNVERIFIED), but no `SKIP LOCKED` or SQL: the claim becomes a transaction that retries on contention, the
  FIFO-with-filters query needs composite indexes, and nothing learned transfers to the PostgreSQL the maintainer
  wants to know.
- **DynamoDB:** the most exam-relevant (ADR 0005 said so), and conditional writes make an atomic claim. But the
  broker runs on Google Cloud, and every call would cross clouds through federation (tens of milliseconds more,
  against 3 ms of SQL), and the oldest-matching-job query needs a GSI per filter combination.
- **Keeping SQLite for local runs and tests:** two stores means the Postgres-only behaviour (SKIP LOCKED, timeouts,
  advisory locks, pool errors) is untested where developers run, and two SQL dialects to keep equal. A Postgres
  container costs seconds.
- **RDS Proxy or PgBouncer:** a pooler lets many instances share few server connections, which matters when
  `instances x pool` exceeds the limit. At 12 of about 25 it solves nothing, and it adds a component to run,
  secure and pay for. Revisit at more than 6 instances, or when workers hold per-worker connections (phase 10).
