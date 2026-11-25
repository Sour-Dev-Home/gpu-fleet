"""The broker's job table, in SQLite.

Every state change is a single SQL statement, and SQLite runs each write under a
database-wide lock, so a statement is atomic: two workers can never claim the same job.

Times are the broker's wall clock (time.time()), passed in as `now` so tests use plain
numbers. Only the broker compares them; workers only ever see durations (ttl_seconds).
"""

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    input_sha256     TEXT NOT NULL,
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'leased', 'done', 'dead')),
    attempts         INTEGER NOT NULL DEFAULT 0,  -- how many times the job was leased
    lease_token      TEXT,                        -- set while leased
    lease_expires_at REAL,                        -- broker wall clock, set while leased
    result_key       TEXT                         -- set when done
);
"""


@dataclass(frozen=True)
class Claim:
    """A job handed to a worker: what to run, plus the lease that proves it's theirs."""

    job_id: str
    input_sha256: str
    model: str
    prompt_version: str
    lease_token: str
    lease_expires_at: float
    attempts: int


class JobStore:
    def __init__(self, path: str | Path) -> None:
        # isolation_level=None: autocommit, so each statement is its own atomic transaction.
        # timeout: wait up to 5 s for another connection's write lock instead of failing.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(
        self, job_id: str, input_sha256: str, model: str, prompt_version: str
    ) -> None:
        """Queue a job. Adding the same job_id again is a no-op (idempotent enqueue)."""
        self.db.execute(
            "INSERT OR IGNORE INTO jobs (job_id, input_sha256, model, prompt_version)"
            " VALUES (?, ?, ?, ?)",
            (job_id, input_sha256, model, prompt_version),
        )

    def get(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()

    def claim(self, now: float, ttl_seconds: float) -> Claim | None:
        """Lease the oldest queued job to the caller, or return None if nothing is queued.

        Must be ONE statement, so no other worker can claim the same job in between.
        """
        # unguessable: only the leaseholder can heartbeat or complete
        token = secrets.token_urlsafe(16)
        row = self.db.execute(
            "UPDATE jobs SET state='leased', lease_token=?, lease_expires_at=?, attempts=attempts+1"
            " WHERE job_id = (SELECT job_id FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1)"
            " RETURNING job_id, input_sha256, model, prompt_version, lease_token, lease_expires_at, attempts",
            (token, now + ttl_seconds),
        ).fetchone()
        if row is None:
            return None
        return Claim(**dict(row))
