# 0016: SQLite busy handling (WAL, 503 with Retry-After, client retries)

Status: Accepted (maintainer, 2026-11-25, Q18)
Date: 2026-11-25

## Context

Under load (3 workers plus `fleet-enqueue --demo 200`) one `POST /jobs` returned HTTP 500. Cause: lock
starvation in SQLite. Every request opens its own connection (5 s busy timeout), and the default rollback
journal makes readers and writers block each other. A waiter polls for the lock and can lose the race for
more than 5 s, so `sqlite3.OperationalError: database is locked` escapes and FastAPI turns it into a 500.
A stress repro (2000 operations, many threads) gave 3-9 lock errors per run in rollback mode, 0-1 in WAL.

A second cause showed up while building this, and was probably behind most of the original 500s: FastAPI
runs a sync dependency's setup and teardown on different pool threads, so `close()` sometimes ran on a
thread other than the one that opened the connection, and sqlite3 raised `ProgrammingError` (18 of 500
requests in a Compose run). Fixed with `check_same_thread=False`: each connection is still used by one
request at a time, only handed between threads. It's a bug fix, not a decision, so it isn't listed below.

## Decision

1. The job store runs in `journal_mode=WAL`, set once at startup in `create_app` (the mode persists in
   the database file). Readers and the single writer no longer block each other.
2. A store `OperationalError` saying the database is locked or busy becomes `503` with `Retry-After: 1`
   and a warning log. Any other `OperationalError` stays a 500 (a real fault). The busy timeout is a
   `create_app` parameter (default 5.0 s) so tests can hold a real lock with a short timeout.
3. `fleet-enqueue` retries a 503 or a connection error (URLError, ConnectionError, timeout) up to 5
   attempts per job: exponential backoff with full jitter, base 0.5 s, capped at 8 s, with `Retry-After`
   as the minimum wait. 4xx is never retried. Safe because `POST /jobs` is `INSERT OR IGNORE`.
   Workers already treat 5xx as transient.

## Consequences

- Starvation becomes rare (WAL) and, when it still happens, a retryable 503 rather than a 500.
- WAL adds `-wal` and `-shm` files next to the database; they must live on the same local volume
  (WAL does not work on network filesystems). Backups must copy all three or use `.backup`.
- Full jitter spreads retrying clients out, so retries do not re-create the stampede.
- A lock held longer than 5 attempts of backoff (about 15 s worst case) still fails the job, loudly:
  `fleet-enqueue` exits non-zero.

## Alternatives considered

- An in-process write lock for fairness: rejected. It is SQLite-specific and does nothing across
  processes; phase 4 replaces SQLite with PostgreSQL (ADR 0005), which has real row-level concurrency.
- A longer busy timeout: rejected. It only delays starvation (waiters still poll and can still lose)
  and ties up request threads while it waits.
