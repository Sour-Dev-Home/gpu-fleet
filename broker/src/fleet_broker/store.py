"""The broker's job table, in PostgreSQL (ADR 0020).

Every state change is a single SQL statement (autocommit), and a claim locks the row it picks
with FOR UPDATE SKIP LOCKED, so two workers can never claim the same job and never wait for
each other either.

Times are the broker's wall clock (time.time()), passed in as `now` so tests use plain
numbers. Only the broker compares them; workers only ever see durations (ttl_seconds).

A connection is held only while SQL runs: every method checks one out of the pool, runs its
statements and gives it back.
"""

import hashlib
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from . import addressing

# The pool's numbers (ADR 0020). 20 requests/s x 3 ms of SQL each is ~0.06 connections busy
# (Little's law), so 4 is generous. max_instances(3) x 4 = 12 stays under Cloud SQL's
# smallest-tier connection limit.
POOL_MAX_SIZE = 4
POOL_TIMEOUT = 2.0  # seconds to wait for a free connection before answering 503
POOL_MAX_LIFETIME = 1800.0  # recycle connections: survives failovers and maintenance restarts
# Session limits, set on every pooled connection (a stuck query or transaction must not hold
# one of four connections for long).
SESSION_SETTINGS = {
    "statement_timeout": "5s",
    "idle_in_transaction_session_timeout": "10s",
    "lock_timeout": "3s",
}
# Arbitrary, fixed: the advisory lock concurrent instances take while migrating.
MIGRATION_LOCK_KEY = 0x666C656574  # "fleet"
MIGRATE_CONNECT_TIMEOUT = 10.0  # at startup the database may still be coming up

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq              BIGINT GENERATED ALWAYS AS IDENTITY,  -- FIFO order (Postgres has no rowid)
    job_id           TEXT PRIMARY KEY,
    input_sha256     TEXT NOT NULL,
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'leased', 'done', 'dead')),
    attempts         INTEGER NOT NULL DEFAULT 0,  -- how many times the job was leased
    lease_token      TEXT,                        -- sha256 of the lease token, set while leased
    lease_expires_at DOUBLE PRECISION,            -- broker wall clock, set while leased
    result_key       TEXT,                        -- set when done
    context_sha256   TEXT,                        -- NULL: no context (ADR 0012)
    data_class       TEXT,                        -- NULL: treated as footage (ADR 0015)
    enqueued_at      DOUBLE PRECISION,            -- broker wall clock; NULL for rows before ADR 0018
    done_at          DOUBLE PRECISION             -- set by the first accepted completion
);
"""

# Columns added after the first release, as (name, SQL type). CREATE TABLE IF NOT EXISTS
# skips a table that already exists, so an older database gets these with ALTER TABLE.
# Expand only (ADR 0012): add nullable columns, never rename or drop, so old rows and older
# code (a rolling deploy runs both) keep working. Postgres adds a nullable column without
# rewriting the table.
MIGRATIONS = [
    ("context_sha256", "TEXT"),
    ("data_class", "TEXT"),
    ("enqueued_at", "DOUBLE PRECISION"),  # ADR 0018: queue age and the enqueue-to-done SLO
    ("done_at", "DOUBLE PRECISION"),
]

# Serves the claim: the oldest available job, in FIFO order, without sorting. Done and dead
# jobs (the bulk of a long-lived table) are not in it.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS jobs_open_seq_idx ON jobs (seq) WHERE state IN ('queued', 'leased')",
]

# The claim: ONE statement. The CTE picks the oldest available job that matches and locks it;
# SKIP LOCKED passes over rows another claim holds instead of waiting, so concurrent workers
# take different jobs. The UPDATE then leases the locked row.
CLAIM_SQL = """
WITH candidate AS (
    SELECT job_id FROM jobs
    WHERE (state = 'queued' OR (state = 'leased' AND lease_expires_at <= %(now)s))
      AND attempts < %(max_attempts)s
      AND COALESCE(data_class, 'footage') = ANY(%(clearance)s)
      AND prompt_version = ANY(%(capabilities)s)
      AND model = ANY(%(models)s)
    ORDER BY seq
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs SET state = 'leased', lease_token = %(token)s,
                lease_expires_at = %(expires)s, attempts = attempts + 1
FROM candidate
WHERE jobs.job_id = candidate.job_id
RETURNING jobs.job_id, input_sha256, model, prompt_version,
          lease_expires_at, attempts, context_sha256
"""


def _digest(lease_token: str) -> str:
    """What the table stores instead of the token (docs/security.md, before phase 4), so a read
    of the database can't take over a lease. Plain SHA-256 is enough: a token has 128 random
    bits, so there's nothing to guess and no salt or slow hash is needed (those protect
    passwords, which people choose)."""
    return hashlib.sha256(lease_token.encode()).hexdigest()


def _configure(conn: Connection) -> None:
    for name, value in SESSION_SETTINGS.items():  # constants above, never input
        conn.execute(f"SET {name} = '{value}'")


def make_pool(
    database_url: str, *, timeout: float = POOL_TIMEOUT, max_size: int = POOL_MAX_SIZE
) -> ConnectionPool:
    """The app's one pool, already open. Closing it is the owner's job (the app's lifespan).

    The URL holds the password: nothing here puts it in a message or a log line."""
    try:
        conninfo_to_dict(database_url)
    except psycopg.Error:
        # libpq's message can quote part of the string: say only that it's malformed
        raise ValueError("the database URL is not a valid PostgreSQL connection string") from None
    pool = ConnectionPool(
        database_url,
        min_size=0,  # no idle connections held while the broker is quiet (it scales to zero)
        max_size=max_size,
        timeout=timeout,
        max_lifetime=POOL_MAX_LIFETIME,
        check=ConnectionPool.check_connection,  # a dead connection is replaced, not handed out
        configure=_configure,
        kwargs={"autocommit": True, "row_factory": dict_row},  # each statement its own transaction
        name="broker",
        open=False,
    )
    pool.open()
    return pool


def is_unavailable(exc: BaseException) -> bool:
    """Is this error "the database can't answer right now", where retrying later can help?

    Yes: no connection (refused, dropped, unknown database), no free pooled connection in time,
    a statement or lock timeout, a restarting or full server. No: everything else (a missing
    table, a constraint, a bug) is a real fault and stays a 500 (ADR 0020, ADR 0016)."""
    if isinstance(exc, PoolTimeout | PoolClosed):
        return True
    if not isinstance(exc, psycopg.OperationalError):
        return False
    state = exc.sqlstate
    return (
        state is None  # client side: connection refused, closed, timed out
        or state.startswith("08")  # connection exception
        or state
        in {
            "57014",  # query_canceled: statement_timeout
            "55P03",  # lock_not_available: lock_timeout
            "57P01",  # admin_shutdown
            "57P02",  # crash_shutdown
            "57P03",  # cannot_connect_now: starting up or shutting down
            "53300",  # too_many_connections
            "40P01",  # deadlock_detected: one side is rolled back, the retry succeeds
            "40001",  # serialization_failure: likewise
        }
    )


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
    context_sha256: str | None = None


@dataclass(frozen=True)
class Completion:
    """An accepted completion. `first` is False for a repeat (a retry after a lost response),
    so a metric counts each job once."""

    model: str
    prompt_version: str
    data_class: str
    enqueued_at: float | None
    done_at: float | None
    first: bool


@dataclass(frozen=True)
class QueueStats:
    """What the queue looks like right now (ADR 0018 gauges)."""

    # (state, data_class) -> job count. state adds 'expired': leased, lease run out, not yet
    # re-leased (lazy expiry keeps such rows in 'leased' until the next claim).
    counts: dict[tuple[str, str], int]
    oldest_available_enqueued_at: float | None  # None: nothing available, or only pre-ADR-0018 rows


def result_key_for(job: dict[str, Any]) -> str:
    return addressing.result_key(
        job["input_sha256"], job["model"], job["prompt_version"], job["context_sha256"]
    )


class ResultKeyMismatch(Exception):
    """The lease holder named a result key that is not the job's content address (ADR 0023)."""


class JobStore:
    """Stateless apart from the pool it was given: one instance serves every request."""

    def __init__(self, pool: ConnectionPool, max_attempts: int = 3, migrate: bool = True) -> None:
        # A job leased this many times without completing is a "poison" job: it goes to
        # the 'dead' state for a human to look at instead of being retried forever.
        self.max_attempts = max_attempts
        self.pool = pool
        if migrate:
            self.migrate()

    def migrate(self) -> None:
        """Create the schema and add missing columns, safe to run on every start and from
        several instances at once: an advisory lock makes them take turns, so none races
        another's CREATE or ALTER. (lock_timeout bounds the wait: a stuck migrator fails the
        next one with a lock error instead of hanging it.) Add-only: see MIGRATIONS."""
        with self.pool.connection(timeout=MIGRATE_CONNECT_TIMEOUT) as conn, conn.transaction():
            # xact lock: released at commit or rollback, so a crash can't leave it held
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            conn.execute(SCHEMA)
            for name, sql_type in MIGRATIONS:
                # name and type come from the constant above, never from input
                conn.execute(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {name} {sql_type}")
            for index in INDEXES:
                conn.execute(index)

    def add(
        self,
        job_id: str,
        input_sha256: str,
        model: str,
        prompt_version: str,
        context_sha256: str | None = None,
        data_class: str | None = None,
        now: float | None = None,
    ) -> None:
        """Queue a job. Adding the same job_id again is a no-op (idempotent enqueue): the
        first enqueue time stays."""
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, input_sha256, model, prompt_version,"
                " context_sha256, data_class, enqueued_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (job_id) DO NOTHING",
                (job_id, input_sha256, model, prompt_version, context_sha256, data_class, now),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone()

    def claim(
        self,
        now: float,
        ttl_seconds: float,
        clearance: Collection[str],
        capabilities: Collection[str],
        models: Collection[str],
    ) -> Claim | None:
        """Lease the oldest available job the caller may and can run, or None if there is none.

        May: the job's data class (NULL counts as footage) is in `clearance`. Can: its prompt
        version is in `capabilities` and its model in `models` (ADR 0015). Other jobs are
        skipped, stay queued and never burn an attempt.

        Available means queued, or leased with an expired lease (lazy expiry: no reaper
        process). Re-leasing gives a new token, so the old worker's heartbeats now fail.
        The claim itself must be ONE statement, so no other worker can take the same job.
        """
        # unguessable: only the leaseholder can heartbeat or complete
        token = secrets.token_urlsafe(16)
        clearance, capabilities, models = list(clearance), list(capabilities), list(models)
        if not (clearance and capabilities and models):
            return None  # nothing can match
        with self.pool.connection() as conn:
            # Dead-letter first: an expired job that used up its attempts must never be
            # claimed again (the claim also skips it). Hands nothing out, so it doesn't need
            # to be atomic with the claim.
            conn.execute(
                "UPDATE jobs SET state = 'dead'"
                " WHERE state = 'leased' AND lease_expires_at <= %s AND attempts >= %s",
                (now, self.max_attempts),
            )
            row = conn.execute(
                CLAIM_SQL,
                {
                    "now": now,
                    "max_attempts": self.max_attempts,
                    "clearance": clearance,
                    "capabilities": capabilities,
                    "models": models,
                    "token": _digest(token),
                    "expires": now + ttl_seconds,
                },
            ).fetchone()
        if row is None:
            return None
        return Claim(**row, lease_token=token)  # the only place the token itself goes

    def heartbeat(
        self, job_id: str, lease_token: str, now: float, ttl_seconds: float
    ) -> float | None:
        """Extend the lease and return its new expiry, or None if the caller lost it.

        The token is ownership: if the job was re-leased (new token), completed, or
        dead-lettered, no row matches and the heartbeat fails. A lease that expired but
        that nobody re-leased yet can still be renewed.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                "UPDATE jobs SET lease_expires_at = %s"
                " WHERE job_id = %s AND lease_token = %s AND state = 'leased'"
                " RETURNING lease_expires_at",
                (now + ttl_seconds, job_id, _digest(lease_token)),
            ).fetchone()
        return None if row is None else row["lease_expires_at"]

    def complete(
        self, job_id: str, lease_token: str, result_key: str, now: float | None = None
    ) -> Completion | None:
        """Mark the job done with its result key. A Completion if accepted, else None.

        Idempotent: repeating the same completion (a retry after a lost response) is
        accepted again, with first=False. A stale token is refused (None).

        The key is recomputed from the job's own stored fields; a different one raises
        ResultKeyMismatch, whether the job is leased or already done, and changes nothing.
        Only the holder of the job's current token is told (a stale token gets None, so
        nobody learns anything about a job they do not hold).
        """
        facts = (
            "model, prompt_version, COALESCE(data_class, 'footage') AS data_class,"
            " enqueued_at, done_at"
        )
        with self.pool.connection() as conn:
            job = conn.execute(
                "SELECT input_sha256, model, prompt_version, context_sha256 FROM jobs"
                " WHERE job_id = %s AND lease_token = %s AND state IN ('leased', 'done')",
                (job_id, _digest(lease_token)),
            ).fetchone()
            if job is not None and result_key_for(job) != result_key:
                raise ResultKeyMismatch(job_id)
            # The first completion moves leased -> done, atomically: only one caller can win it.
            # (Telling "first" apart by time would miscount a retry within the same clock tick.)
            row = conn.execute(
                "UPDATE jobs SET state = 'done', result_key = %s, done_at = %s"
                " WHERE job_id = %s AND lease_token = %s AND state = 'leased'"
                f" RETURNING {facts}",
                (result_key, now, job_id, _digest(lease_token)),
            ).fetchone()
            if row is not None:
                return Completion(**row, first=True)
            # A repeat of the same completion: accepted, nothing to write.
            row = conn.execute(
                f"SELECT {facts} FROM jobs"
                " WHERE job_id = %s AND lease_token = %s AND state = 'done' AND result_key = %s",
                (job_id, _digest(lease_token), result_key),
            ).fetchone()
        return None if row is None else Completion(**row, first=False)

    def stats(self, now: float) -> QueueStats:
        """Counts by state and data class, and when the oldest available job was enqueued.

        Read-only, two aggregate queries: cheap enough to run on every metrics export."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT CASE WHEN state = 'leased' AND lease_expires_at <= %s THEN 'expired'"
                "   ELSE state END AS state,"
                " COALESCE(data_class, 'footage') AS data_class, COUNT(*) AS n"
                " FROM jobs GROUP BY 1, 2",
                (now,),
            ).fetchall()
            oldest = conn.execute(
                "SELECT MIN(enqueued_at) AS oldest FROM jobs"
                " WHERE state = 'queued' OR (state = 'leased' AND lease_expires_at <= %s)",
                (now,),
            ).fetchone()
        counts = {(row["state"], row["data_class"]): row["n"] for row in rows}
        return QueueStats(counts, oldest["oldest"])
