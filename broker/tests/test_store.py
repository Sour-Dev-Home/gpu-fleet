import hashlib
import threading
import time

import psycopg
import pytest
from fleet_broker.addressing import result_key
from fleet_broker.store import (
    CLAIM_SQL,
    MIGRATION_LOCK_KEY,
    JobStore,
    make_pool,
)

NOW = 1_800_000_000.0  # a fixed wall-clock time; only differences matter
# Clears everything: these tests are about leases, not about who may take which job (test_clearance.py).
ANY = {
    "clearance": {"footage", "derived", "open"},
    "capabilities": {"describe-v1", "v1"},
    "models": {"qwen3-vl:8b", "m"},
}


def store_with(open_store, n: int) -> JobStore:
    store = open_store()
    for i in range(n):
        store.add(f"j{i}", "a" * 64, "qwen3-vl:8b", "describe-v1")
    return store


def test_add_is_idempotent(open_store):
    store = store_with(open_store, 1)
    store.add("j0", "b" * 64, "other", "other")  # same id again: ignored
    assert store.get("j0")["model"] == "qwen3-vl:8b"


def test_claim_on_empty_queue_returns_none(open_store):
    assert store_with(open_store, 0).claim(NOW, 60, **ANY) is None


def test_claim_leases_oldest_job(open_store):
    store = store_with(open_store, 2)
    claim = store.claim(NOW, 60, **ANY)
    assert claim.job_id == "j0"
    assert claim.lease_expires_at == NOW + 60  # a double, not a float4: epoch seconds fit
    assert claim.attempts == 1
    row = store.get("j0")
    assert row["state"] == "leased"
    assert row["lease_token"] == hashlib.sha256(claim.lease_token.encode()).hexdigest()


def test_fifo_order_is_enqueue_order_not_job_id_order(open_store):
    store = open_store()
    for job_id in ("zz", "aa", "mm"):
        store.add(job_id, "a" * 64, "m", "v1")
    assert [store.claim(NOW, 60, **ANY).job_id for _ in range(3)] == ["zz", "aa", "mm"]


def test_a_copy_of_the_database_cannot_take_over_a_lease(open_store):
    # Only sha256(token) is stored (docs/security.md, before phase 4): someone who reads the
    # table learns the hash, which the broker won't accept as a token.
    store = store_with(open_store, 1)
    claim = store.claim(NOW, 60, **ANY)
    stolen = store.get("j0")["lease_token"]
    assert claim.lease_token not in {stolen, None}
    assert store.heartbeat("j0", stolen, NOW, 60) is None
    key = result_key("a" * 64, "qwen3-vl:8b", "describe-v1")
    assert store.complete("j0", stolen, key, now=NOW) is None
    assert store.heartbeat("j0", claim.lease_token, NOW, 60) == NOW + 60
    assert store.complete("j0", claim.lease_token, key, now=NOW).first


def test_claimed_job_is_not_claimed_again(open_store):
    store = store_with(open_store, 2)
    assert [store.claim(NOW, 60, **ANY).job_id for _ in range(2)] == ["j0", "j1"]
    assert store.claim(NOW, 60, **ANY) is None


def test_tokens_are_unique(open_store):
    store = store_with(open_store, 2)
    assert store.claim(NOW, 60, **ANY).lease_token != store.claim(NOW, 60, **ANY).lease_token


def test_concurrent_claims_never_share_a_job(open_store):
    # 8 workers, each with its own pool (like separate broker instances), race for 50 jobs.
    store_with(open_store, 50)
    claimed: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []
    stores = [open_store(migrate=False) for _ in range(8)]
    start = threading.Barrier(8)

    def worker(mine: JobStore):
        try:
            start.wait()
            while (claim := mine.claim(NOW, 60, **ANY)) is not None:
                with lock:
                    claimed.append(claim.job_id)
        except Exception as e:  # noqa: BLE001 a thread's error must fail the test, not vanish
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # each job claimed exactly once
    assert sorted(claimed) == sorted(f"j{i}" for i in range(50))


def test_claim_skips_a_job_another_connection_has_locked_instead_of_waiting(db_url, open_store):
    # FOR UPDATE SKIP LOCKED: a row somebody else is working on is passed over, so one slow
    # statement can't stall every worker's lease (without it this would wait for the lock).
    store = store_with(open_store, 3)
    with psycopg.connect(db_url) as other:  # not autocommit: the lock lasts until we leave
        other.execute("SELECT 1 FROM jobs WHERE job_id = 'j0' FOR UPDATE")
        started = time.monotonic()
        claim = store.claim(NOW, 60, **ANY)
        assert time.monotonic() - started < 1.0
        assert claim.job_id == "j1"
        assert store.claim(NOW, 60, **ANY).job_id == "j2"
        assert store.claim(NOW, 60, **ANY) is None  # j0 is locked, not available
    assert store.claim(NOW, 60, **ANY).job_id == "j0"  # unlocked: the oldest again


def test_the_claim_statement_itself_never_takes_a_job_that_used_up_its_attempts(open_store):
    # The dead-lettering UPDATE runs just before the claim, but a job must never be re-leased
    # in the window between two statements either: the claim's own WHERE refuses it.
    store = store_with(open_store, 1)
    store.claim(NOW, 60, **ANY)
    store.claim(NOW + 100, 60, **ANY)
    store.claim(NOW + 200, 60, **ANY)  # third lease: attempts == max_attempts
    params = {
        "now": NOW + 300,
        "max_attempts": 3,
        "clearance": ["footage"],
        "capabilities": ["describe-v1"],
        "models": ["qwen3-vl:8b"],
        "token": "t",
        "expires": NOW + 360,
    }
    with store.pool.connection() as conn:  # the claim alone, without the dead-lettering before it
        assert conn.execute(CLAIM_SQL, params).fetchone() is None


def test_the_claim_is_served_by_the_partial_index(db_url, open_store):
    # EXPLAIN: the candidate scan walks jobs_open_seq_idx in FIFO order (no sort), and that
    # index holds only queued and leased rows, so finished jobs don't slow the claim.
    store = store_with(open_store, 300)
    with store.pool.connection() as conn:
        conn.execute("UPDATE jobs SET state = 'done' WHERE seq <= 250")
        conn.execute("ANALYZE jobs")
        with conn.transaction():
            # 300 rows: Postgres would scan the table, or bitmap-scan and sort the 50 open ones.
            # Both are fine at this size. Forbidding them asks the real question: CAN the index
            # hand over the oldest open job in order, so a long backlog is never sorted?
            conn.execute("SET LOCAL enable_seqscan = off")
            conn.execute("SET LOCAL enable_bitmapscan = off")
            conn.execute("SET LOCAL enable_sort = off")
            params = {
                "now": NOW,
                "max_attempts": 3,
                "clearance": ["footage"],  # the jobs' NULL data class counts as footage
                "capabilities": ["describe-v1"],
                "models": ["qwen3-vl:8b"],
                "token": "t",
                "expires": NOW + 60,
            }
            cur = psycopg.ClientCursor(conn)  # EXPLAIN with the values inlined
            plan = "\n".join(
                next(iter(r.values()))
                for r in cur.execute("EXPLAIN " + CLAIM_SQL, params).fetchall()
            )
    assert "jobs_open_seq_idx" in plan, plan
    assert "Sort" not in plan, plan


# --- pool and session settings (ADR 0020) ---


def test_pool_numbers(db_url):
    pool = make_pool(db_url)
    try:
        assert (pool.min_size, pool.max_size) == (0, 4)
        assert pool.timeout == 2.0
        assert pool.max_lifetime == 1800
        with pool.connection() as conn:
            assert conn.autocommit
    finally:
        pool.close()


def test_every_pooled_connection_has_the_session_timeouts(db_url):
    pool = make_pool(db_url)
    try:
        for _ in range(2):  # the first checkout creates the connection, the second reuses it
            with pool.connection() as conn:
                got = {
                    name: conn.execute(f"SHOW {name}").fetchone()[name]
                    for name in (
                        "statement_timeout",
                        "idle_in_transaction_session_timeout",
                        "lock_timeout",
                    )
                }
            assert got == {
                "statement_timeout": "5s",
                "idle_in_transaction_session_timeout": "10s",
                "lock_timeout": "3s",
            }
    finally:
        pool.close()


def test_a_connection_that_died_while_pooled_is_replaced_on_checkout(db_url, open_store):
    store = store_with(open_store, 1)  # the pool now holds one idle connection
    with psycopg.connect(db_url, autocommit=True) as killer:
        killer.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        for _ in range(50):  # the kill is asynchronous: wait until the backends are gone
            n = killer.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            ).fetchone()[0]
            if n == 0:
                break
            time.sleep(0.05)
    assert store.get("j0")["job_id"] == "j0"  # no error: the dead connection was not handed out


def test_a_connection_is_held_only_while_sql_runs(open_store):
    store = store_with(open_store, 1)
    store.claim(NOW, 60, **ANY)
    assert store.pool.get_stats()["pool_available"] >= 1  # back in the pool, not kept by the store
    assert store.pool.get_stats()["pool_size"] == store.pool.get_stats()["pool_available"]


def test_a_bad_url_is_refused_without_quoting_it():
    # libpq's own message quotes the string it could not parse: ours must not
    with pytest.raises(ValueError) as e:
        make_pool("postgresql://u:hunter2-secret@[bad/db")
    assert "hunter2" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


# --- schema and migration (ADR 0012, ADR 0020) ---

OLD_SCHEMA = """
CREATE TABLE jobs (
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    job_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, model TEXT NOT NULL,
    prompt_version TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires_at DOUBLE PRECISION,
    result_key TEXT
);
INSERT INTO jobs (job_id, input_sha256, model, prompt_version) VALUES ('old', 'a', 'm', 'v1');
"""


def test_an_older_database_gains_the_columns_and_keeps_its_jobs(db_url, open_store):
    with psycopg.connect(db_url, autocommit=True) as db:
        db.execute(OLD_SCHEMA)

    s = open_store()  # opening it runs the migration
    claim = s.claim(now=1000.0, ttl_seconds=60, **ANY)
    assert claim.job_id == "old"
    assert claim.context_sha256 is None  # old rows read as "no context"
    assert {"context_sha256", "data_class", "enqueued_at", "done_at"} <= set(s.get("old"))
    open_store()  # a second open must not try to add the columns again


def test_migrating_creates_the_claim_index(open_store):
    s = open_store()
    with s.pool.connection() as conn:
        names = {r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes")}
    assert "jobs_open_seq_idx" in names


def test_instances_starting_at_the_same_time_all_migrate(open_store):
    # Two or more brokers start together (a rollout, a scale-out): each runs the migration on
    # the same empty database. Under the advisory lock they take turns and all succeed.
    n = 6
    start = threading.Barrier(n)
    errors: list[BaseException] = []

    def boot():
        try:
            start.wait()
            open_store()
        except Exception as e:  # noqa: BLE001 a thread's error must fail the test, not vanish
            errors.append(e)

    threads = [threading.Thread(target=boot) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_a_migration_waits_while_another_instance_holds_the_migration_lock(db_url, open_store):
    # Deterministic version of the race above: with the lock held elsewhere, migrating must
    # wait for it (and not touch the schema), then carry on once it is released.
    done = threading.Event()
    errors: list[BaseException] = []

    def migrate():
        try:
            open_store()
        except Exception as e:  # noqa: BLE001 a thread's error must fail the test, not vanish
            errors.append(e)
        done.set()

    with psycopg.connect(db_url, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        t = threading.Thread(target=migrate)
        t.start()
        assert not done.wait(0.7), "the migration ran while another instance held the lock"
        tables = holder.execute("SELECT to_regclass('jobs')").fetchone()[0]
        assert tables is None
        holder.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    assert done.wait(5)
    t.join()
    assert errors == []


def test_a_migration_gives_up_instead_of_waiting_forever_for_the_lock(db_url, open_store):
    # lock_timeout (3 s) bounds the wait: a stuck migrator fails the next one with a lock error,
    # which the broker's startup shows, instead of hanging it.
    with psycopg.connect(db_url, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            open_store()
        assert 2.5 < time.monotonic() - started < 6


def test_context_round_trips_through_a_claim(open_store):
    s = open_store()
    s.add("j", "a" * 64, "m", "v1", context_sha256="c" * 64)
    assert s.claim(now=1000.0, ttl_seconds=60, **ANY).context_sha256 == "c" * 64


def test_a_store_works_from_several_threads_at_once(open_store):
    # FastAPI runs sync endpoints on a thread pool: one store, many threads, a pool of 4.
    s = store_with(open_store, 20)
    errors = []

    def use():
        try:
            for _ in range(10):
                s.stats(NOW)
                s.get("j0")
        except Exception as e:  # noqa: BLE001 a thread's error must fail the test, not vanish
            errors.append(e)

    threads = [threading.Thread(target=use) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
