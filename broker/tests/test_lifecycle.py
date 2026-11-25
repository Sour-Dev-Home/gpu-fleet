from fleet_broker.store import JobStore

NOW = 1_800_000_000.0
TTL = 60.0
KEY = "f" * 64  # a result key


def store_with(tmp_path, n: int = 1, max_attempts: int = 3) -> JobStore:
    store = JobStore(tmp_path / "broker.db", max_attempts=max_attempts)
    for i in range(n):
        store.add(f"j{i}", "a" * 64, "qwen3-vl:8b", "describe-v1")
    return store


# --- heartbeat (safe update: job + token + state must still match) ---


def test_heartbeat_extends_the_lease(tmp_path):
    store = store_with(tmp_path)
    c = store.claim(NOW, TTL)
    assert store.heartbeat("j0", c.lease_token, NOW + 40, TTL) == NOW + 40 + TTL


def test_heartbeat_with_wrong_token_is_refused(tmp_path):
    store = store_with(tmp_path)
    store.claim(NOW, TTL)
    assert store.heartbeat("j0", "not-the-token", NOW + 40, TTL) is None


def test_heartbeat_after_complete_is_refused(tmp_path):
    store = store_with(tmp_path)
    c = store.claim(NOW, TTL)
    store.complete("j0", c.lease_token, KEY)
    assert store.heartbeat("j0", c.lease_token, NOW + 40, TTL) is None


def test_expired_lease_nobody_took_can_still_be_renewed(tmp_path):
    store = store_with(tmp_path)
    c = store.claim(NOW, TTL)
    assert store.heartbeat("j0", c.lease_token, NOW + 90, TTL) == NOW + 90 + TTL


# --- complete (idempotent) ---


def test_complete_marks_done_with_result_key(tmp_path):
    store = store_with(tmp_path)
    c = store.claim(NOW, TTL)
    assert store.complete("j0", c.lease_token, KEY)
    row = store.get("j0")
    assert (row["state"], row["result_key"]) == ("done", KEY)


def test_complete_with_wrong_token_is_refused(tmp_path):
    store = store_with(tmp_path)
    store.claim(NOW, TTL)
    assert not store.complete("j0", "not-the-token", KEY)
    assert store.get("j0")["state"] == "leased"


def test_complete_retry_is_accepted_but_a_different_result_is_not(tmp_path):
    store = store_with(tmp_path)
    c = store.claim(NOW, TTL)
    assert store.complete("j0", c.lease_token, KEY)
    assert store.complete("j0", c.lease_token, KEY)  # response was lost; worker retries
    assert not store.complete("j0", c.lease_token, "0" * 64)


# --- expiry and dead letters (your claim() changes) ---


def test_live_lease_is_not_released(tmp_path):
    store = store_with(tmp_path)
    store.claim(NOW, TTL)
    assert store.claim(NOW + 59, TTL) is None


def test_expired_lease_is_released_with_a_new_token(tmp_path):
    store = store_with(tmp_path)
    first = store.claim(NOW, TTL)
    second = store.claim(NOW + TTL, TTL)  # exactly at expiry
    assert second is not None and second.job_id == "j0"
    assert second.lease_token != first.lease_token
    assert second.attempts == 2


def test_old_worker_loses_the_job_once_it_is_released(tmp_path):
    store = store_with(tmp_path)
    first = store.claim(NOW, TTL)
    store.claim(NOW + 61, TTL)
    assert store.heartbeat("j0", first.lease_token, NOW + 62, TTL) is None
    assert not store.complete("j0", first.lease_token, KEY)


def test_done_job_is_never_released(tmp_path):
    store = store_with(tmp_path)
    c = store.claim(NOW, TTL)
    store.complete("j0", c.lease_token, KEY)
    assert store.claim(NOW + 1000, TTL) is None


def test_poison_job_goes_dead_after_max_attempts(tmp_path):
    store = store_with(tmp_path, max_attempts=3)
    t = NOW
    for attempt in (1, 2, 3):
        assert store.claim(t, TTL).attempts == attempt
        t += TTL + 1  # the worker dies; the lease runs out
    assert store.claim(t, TTL) is None
    assert store.get("j0")["state"] == "dead"


def test_exhausted_job_goes_dead_at_exactly_its_expiry(tmp_path):
    # dead-letter and claim must agree on "expired": if one used < and the other <=,
    # a job on its last attempt would be re-leased at exactly its expiry second
    store = store_with(tmp_path, max_attempts=1)
    store.claim(NOW, TTL)
    assert store.claim(NOW + TTL, TTL) is None
    assert store.get("j0")["state"] == "dead"


def test_exhausted_job_is_skipped_for_the_next_one(tmp_path):
    # j0 is first in line, expired, and out of attempts: the claim must move on to j1
    store = store_with(tmp_path, n=2, max_attempts=1)
    assert store.claim(NOW, TTL).job_id == "j0"
    assert store.claim(NOW + 100, TTL).job_id == "j1"
    assert store.get("j0")["state"] == "dead"
