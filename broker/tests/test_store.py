import hashlib
import sqlite3
import threading

import pytest
from fleet_broker.store import JobStore

NOW = 1_800_000_000.0  # a fixed wall-clock time; only differences matter
# Clears everything: these tests are about leases, not about who may take which job (test_clearance.py).
ANY = {
    "clearance": {"footage", "derived", "open"},
    "capabilities": {"describe-v1", "v1"},
    "models": {"qwen3-vl:8b", "m"},
}


def store_with(tmp_path, n: int) -> JobStore:
    store = JobStore(tmp_path / "broker.db")
    for i in range(n):
        store.add(f"j{i}", "a" * 64, "qwen3-vl:8b", "describe-v1")
    return store


def test_add_is_idempotent(tmp_path):
    store = store_with(tmp_path, 1)
    store.add("j0", "b" * 64, "other", "other")  # same id again: ignored
    assert store.get("j0")["model"] == "qwen3-vl:8b"


def test_claim_on_empty_queue_returns_none(tmp_path):
    assert store_with(tmp_path, 0).claim(NOW, 60, **ANY) is None


def test_claim_leases_oldest_job(tmp_path):
    store = store_with(tmp_path, 2)
    claim = store.claim(NOW, 60, **ANY)
    assert claim.job_id == "j0"
    assert claim.lease_expires_at == NOW + 60
    assert claim.attempts == 1
    row = store.get("j0")
    assert row["state"] == "leased"
    assert row["lease_token"] == hashlib.sha256(claim.lease_token.encode()).hexdigest()


def test_a_copy_of_the_database_cannot_take_over_a_lease(tmp_path):
    # Only sha256(token) is stored (docs/security.md, before phase 4): someone who reads the
    # table learns the hash, which the broker won't accept as a token.
    store = store_with(tmp_path, 1)
    claim = store.claim(NOW, 60, **ANY)
    stolen = store.get("j0")["lease_token"]
    assert claim.lease_token not in {stolen, None}
    assert store.heartbeat("j0", stolen, NOW, 60) is None
    assert store.complete("j0", stolen, "f" * 64, now=NOW) is None
    assert store.heartbeat("j0", claim.lease_token, NOW, 60) == NOW + 60
    assert store.complete("j0", claim.lease_token, "f" * 64, now=NOW).first


def test_tokens_stored_before_hashing_are_hashed_on_startup(tmp_path):
    # A database from before the change holds the token itself; the next start hashes it, so
    # no token stays at rest and the running lease still works.
    store = store_with(tmp_path, 1)
    claim = store.claim(NOW, 60, **ANY)
    store.db.execute("UPDATE jobs SET lease_token = ?", (claim.lease_token,))  # the old format
    store.close()
    reopened = JobStore(tmp_path / "broker.db")
    assert (
        reopened.get("j0")["lease_token"] == hashlib.sha256(claim.lease_token.encode()).hexdigest()
    )
    assert reopened.heartbeat("j0", claim.lease_token, NOW, 60) == NOW + 60
    JobStore(tmp_path / "broker.db").close()  # again: nothing left to hash, nothing changes
    assert reopened.heartbeat("j0", claim.lease_token, NOW, 60) == NOW + 60


def test_claimed_job_is_not_claimed_again(tmp_path):
    store = store_with(tmp_path, 2)
    assert [store.claim(NOW, 60, **ANY).job_id for _ in range(2)] == ["j0", "j1"]
    assert store.claim(NOW, 60, **ANY) is None


def test_tokens_are_unique(tmp_path):
    store = store_with(tmp_path, 2)
    assert store.claim(NOW, 60, **ANY).lease_token != store.claim(NOW, 60, **ANY).lease_token


def test_concurrent_claims_never_share_a_job(tmp_path):
    # 8 workers, each with its own connection (like separate broker requests), race for 50 jobs.
    store_with(tmp_path, 50)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker():
        mine = JobStore(tmp_path / "broker.db")
        while (claim := mine.claim(NOW, 60, **ANY)) is not None:
            with lock:
                claimed.append(claim.job_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # each job claimed exactly once
    assert sorted(claimed) == sorted(f"j{i}" for i in range(50))


# --- schema migration (ADR 0012) ---

OLD_SCHEMA = """
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, model TEXT NOT NULL,
    prompt_version TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires_at REAL,
    result_key TEXT
);
INSERT INTO jobs (job_id, input_sha256, model, prompt_version) VALUES ('old', 'a', 'm', 'v1');
"""


def test_an_older_database_gains_the_column_and_keeps_its_jobs(tmp_path):
    path = tmp_path / "broker.db"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.close()

    s = JobStore(path)  # opening it runs the migration
    claim = s.claim(now=1000.0, ttl_seconds=60, **ANY)
    assert claim.job_id == "old"
    assert claim.context_sha256 is None  # old rows read as "no context"
    s.close()
    JobStore(path).close()  # a second open must not try to add the column again


def test_context_round_trips_through_a_claim(tmp_path):
    s = JobStore(tmp_path / "broker.db")
    s.add("j", "a" * 64, "m", "v1", context_sha256="c" * 64)
    assert s.claim(now=1000.0, ttl_seconds=60, **ANY).context_sha256 == "c" * 64


def test_a_store_can_be_closed_from_another_thread(tmp_path):
    # FastAPI runs a sync dependency's setup and teardown on different pool threads, so the
    # per-request store is created on one thread and closed on another (found under load).
    s = JobStore(tmp_path / "broker.db")
    errors = []

    def close():
        try:
            s.close()
        except sqlite3.ProgrammingError as e:
            errors.append(e)

    t = threading.Thread(target=close)
    t.start()
    t.join()
    assert errors == []


def test_wal_that_does_not_take_effect_is_an_error():
    # An in-memory database (like a filesystem without WAL support) answers "memory", not "wal":
    # failing at startup beats running quietly in the mode that starved writers (ADR 0016).
    with pytest.raises(RuntimeError, match="journal_mode"):
        JobStore(":memory:", wal=True)
