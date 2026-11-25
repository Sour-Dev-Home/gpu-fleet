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
    def __init__(self, path: str | Path, max_attempts: int = 3) -> None:
        # A job leased this many times without completing is a "poison" job: it goes to
        # the 'dead' state for a human to look at instead of being retried forever.
        self.max_attempts = max_attempts
        # isolation_level=None: autocommit, so each statement is its own atomic transaction.
        # timeout: wait up to 5 s for another connection's write lock instead of failing.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, job_id: str, input_sha256: str, model: str, prompt_version: str) -> None:
        """Queue a job. Adding the same job_id again is a no-op (idempotent enqueue)."""
        self.db.execute(
            "INSERT OR IGNORE INTO jobs (job_id, input_sha256, model, prompt_version)"
            " VALUES (?, ?, ?, ?)",
            (job_id, input_sha256, model, prompt_version),
        )

    def get(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def claim(self, now: float, ttl_seconds: float) -> Claim | None:
        """Lease the oldest available job to the caller, or return None if there is none.

        Available means queued, or leased with an expired lease (lazy expiry: no reaper
        process). Re-leasing gives a new token, so the old worker's heartbeats now fail.
        The claim itself must be ONE statement, so no other worker can take the same job.
        """
        # unguessable: only the leaseholder can heartbeat or complete
        token = secrets.token_urlsafe(16)
        # Dead-letter first: an expired job that used up its attempts must never be
        # claimed again. Hands nothing out, so it doesn't need to be atomic with the claim.
        self.db.execute(
            "UPDATE jobs SET state='dead'"
            " WHERE state='leased' AND lease_expires_at <= ? AND attempts >= ?",
            (now, self.max_attempts),
        )
        row = self.db.execute(
            "UPDATE jobs SET state='leased', lease_token=?, lease_expires_at=?, attempts=attempts+1"
            " WHERE job_id = (SELECT job_id FROM jobs"
            "   WHERE state='queued' OR (state='leased' AND lease_expires_at <= ?)"
            "   ORDER BY rowid LIMIT 1)"
            " RETURNING job_id, input_sha256, model, prompt_version, lease_token, lease_expires_at, attempts",
            (token, now + ttl_seconds, now),
        ).fetchone()
        if row is None:
            return None
        return Claim(**dict(row))

    def heartbeat(
        self, job_id: str, lease_token: str, now: float, ttl_seconds: float
    ) -> float | None:
        """Extend the lease and return its new expiry, or None if the caller lost it.

        The token is ownership: if the job was re-leased (new token), completed, or
        dead-lettered, no row matches and the heartbeat fails. A lease that expired but
        that nobody re-leased yet can still be renewed.
        """
        row = self.db.execute(
            "UPDATE jobs SET lease_expires_at = ?"
            " WHERE job_id = ? AND lease_token = ? AND state = 'leased'"
            " RETURNING lease_expires_at",
            (now + ttl_seconds, job_id, lease_token),
        ).fetchone()
        return None if row is None else row["lease_expires_at"]

    def complete(self, job_id: str, lease_token: str, result_key: str) -> bool:
        """Mark the job done with its result key. True if accepted.

        Idempotent: repeating the same completion (a retry after a lost response) is
        accepted again. A stale token, or a different result key for a done job, is not.
        """
        cur = self.db.execute(
            "UPDATE jobs SET state = 'done', result_key = ?"
            " WHERE job_id = ? AND lease_token = ?"
            " AND (state = 'leased' OR (state = 'done' AND result_key = ?))",
            (result_key, job_id, lease_token, result_key),
        )
        return cur.rowcount == 1
