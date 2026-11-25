# 0005. SQLite job store for the local phases; one-statement atomic claim

**Status:** Accepted (maintainer's choice, 2026-11-25). To be revisited in phase 4.
**Date:** 2026-11-25

## Context
The broker needs durable job state with an atomic claim. Napkin math: about 5,000 jobs and about 10 workers, at
about 1.5 requests per second at peak. That's under 1% of what one SQLite file handles.

## Decision
- Jobs live in one SQLite table behind `JobStore`.
- `claim()` is **one** `UPDATE … WHERE job_id = (SELECT … ORDER BY rowid LIMIT 1) RETURNING …`. SQLite runs each
  write under a database-wide lock, so two workers can never claim the same job.
- `add()` is idempotent: `INSERT OR IGNORE`.
- The connection runs in autocommit mode. The HTTP layer opens one connection per request, because connections
  must not be shared across threads.

## Consequences
- Fast, offline tests, including an 8-thread race test. A two-statement SELECT-then-UPDATE claim fails it every time.
- The data survives broker restarts.
- It **won't work on Cloud Run** (several short-lived instances, no shared disk). Phase 4 needs a networked store
  (candidates: Firestore, DynamoDB, Postgres) and a new ADR. The `JobStore` interface keeps that swap local.
- Proxies and read replicas aren't needed at this scale. A replica would even reintroduce the double-claim bug,
  because replicas lag.

## Alternatives considered
- **An in-memory dict:** lost on restart, and a lock hides the real atomicity problem.
- **DynamoDB from the start:** the most exam-relevant option (conditional writes), but it needs the phase 3 AWS
  setup and network access in every test.
