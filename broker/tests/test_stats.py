"""Queue statistics and completion facts behind the ADR 0018 metrics."""

import psycopg
from fleet_broker.store import JobStore

NOW = 1_800_000_000.0
TTL = 60.0
KEY = "f" * 64
ANY = {"clearance": {"footage", "derived", "open"}, "capabilities": {"v1"}, "models": {"m"}}


def store(open_store, max_attempts: int = 3) -> JobStore:
    return open_store(max_attempts=max_attempts)


def test_counts_by_state_and_data_class(open_store):
    s = store(open_store)
    s.add("a", "a" * 64, "m", "v1", data_class="open", now=NOW)
    s.add("b", "b" * 64, "m", "v1", data_class="open", now=NOW)
    s.add("c", "c" * 64, "m", "v1", now=NOW)  # no data class: counted as footage
    s.claim(NOW, TTL, **ANY)  # leases "a" (oldest first)
    assert s.stats(NOW).counts == {
        ("leased", "open"): 1,
        ("queued", "open"): 1,
        ("queued", "footage"): 1,
    }


def test_a_lease_that_ran_out_counts_as_expired_until_re_leased(open_store):
    s = store(open_store)
    s.add("a", "a" * 64, "m", "v1", data_class="open", now=NOW)
    s.claim(NOW, TTL, **ANY)
    assert s.stats(NOW + TTL).counts == {("expired", "open"): 1}


def test_oldest_available_job_includes_expired_leases_and_skips_running_ones(open_store):
    s = store(open_store)
    s.add("old", "a" * 64, "m", "v1", now=NOW)
    s.add("new", "b" * 64, "m", "v1", now=NOW + 100)
    s.claim(NOW + 100, TTL, **ANY)  # "old" is now running: not waiting
    assert s.stats(NOW + 100).oldest_available_enqueued_at == NOW + 100
    # its lease runs out: it's waiting again, and it has waited longest
    assert s.stats(NOW + 100 + TTL).oldest_available_enqueued_at == NOW


def test_nothing_available_means_no_oldest(open_store):
    assert store(open_store).stats(NOW).oldest_available_enqueued_at is None


def test_re_enqueue_keeps_the_first_enqueue_time(open_store):
    s = store(open_store)
    s.add("a", "a" * 64, "m", "v1", now=NOW)
    s.add("a", "a" * 64, "m", "v1", now=NOW + 500)  # idempotent retry from fleet-enqueue
    assert s.get("a")["enqueued_at"] == NOW


def test_completion_reports_its_facts_once(open_store):
    s = store(open_store)
    s.add("a", "a" * 64, "m", "v1", data_class="open", now=NOW)
    c = s.claim(NOW, TTL, **ANY)
    done = s.complete("a", c.lease_token, KEY, now=NOW + 30)
    assert done.first and done.enqueued_at == NOW and done.done_at == NOW + 30
    assert (done.model, done.prompt_version, done.data_class) == ("m", "v1", "open")
    # A retry after a lost response is accepted, but must not be counted twice.
    again = s.complete("a", c.lease_token, KEY, now=NOW + 31)
    assert again and not again.first and again.done_at == NOW + 30


def test_refused_completion_is_none(open_store):
    s = store(open_store)
    s.add("a", "a" * 64, "m", "v1", now=NOW)
    s.claim(NOW, TTL, **ANY)
    assert s.complete("a", "not-the-token", KEY, now=NOW) is None


def test_old_database_gains_the_timestamp_columns(db_url, open_store):
    with psycopg.connect(db_url, autocommit=True) as old:
        old.execute(
            "CREATE TABLE jobs (seq BIGINT GENERATED ALWAYS AS IDENTITY, job_id TEXT PRIMARY KEY,"
            " input_sha256 TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,"
            " state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,"
            " lease_token TEXT, lease_expires_at DOUBLE PRECISION, result_key TEXT)"
        )
        old.execute(
            "INSERT INTO jobs (job_id, input_sha256, model, prompt_version)"
            " VALUES ('x','a','m','v1')"
        )
    s = open_store()
    assert s.get("x")["enqueued_at"] is None  # old rows: unknown, not "now"
    assert s.stats(NOW).oldest_available_enqueued_at is None  # MIN skips NULL
