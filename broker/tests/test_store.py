import threading

from fleet_broker.store import JobStore

NOW = 1_800_000_000.0  # a fixed wall-clock time; only differences matter


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
    assert store_with(tmp_path, 0).claim(NOW, 60) is None


def test_claim_leases_oldest_job(tmp_path):
    store = store_with(tmp_path, 2)
    claim = store.claim(NOW, 60)
    assert claim.job_id == "j0"
    assert claim.lease_expires_at == NOW + 60
    assert claim.attempts == 1
    row = store.get("j0")
    assert row["state"] == "leased"
    assert row["lease_token"] == claim.lease_token


def test_claimed_job_is_not_claimed_again(tmp_path):
    store = store_with(tmp_path, 2)
    assert [store.claim(NOW, 60).job_id for _ in range(2)] == ["j0", "j1"]
    assert store.claim(NOW, 60) is None


def test_tokens_are_unique(tmp_path):
    store = store_with(tmp_path, 2)
    assert store.claim(NOW, 60).lease_token != store.claim(NOW, 60).lease_token


def test_concurrent_claims_never_share_a_job(tmp_path):
    # 8 workers, each with its own connection (like separate broker requests), race for 50 jobs.
    store_with(tmp_path, 50)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker():
        mine = JobStore(tmp_path / "broker.db")
        while (claim := mine.claim(NOW, 60)) is not None:
            with lock:
                claimed.append(claim.job_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # each job claimed exactly once
    assert sorted(claimed) == sorted(f"j{i}" for i in range(50))
