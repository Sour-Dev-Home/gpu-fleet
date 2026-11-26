"""POST /jobs/{job_id}/cancel (Q86, ADR 0026): the admin can stop a queued or leased job."""

import psycopg
import pytest
from conftest import tracked_pool
from fastapi.testclient import TestClient
from fleet_broker import store as store_module
from fleet_broker.addressing import result_key
from fleet_broker.app import create_app
from fleet_broker.store import JobStore
from tokens import ADMIN, WORKER

NOW = 1_800_000_000.0
TTL = 60.0
ALL = {"footage", "derived", "open"}
ANY = {"clearance": ALL, "capabilities": {"v1"}, "models": {"m"}}
JOB = {
    "job_id": "j",
    "input_sha256": "a" * 64,
    "model": "m",
    "prompt_version": "v1",
    "data_class": "open",
}
LEASE = {"capabilities": ["v1"], "models": ["m"]}
KEY = result_key("a" * 64, "m", "v1")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def client(db_url, token, clock=None):
    app = create_app(db_url, WORKER, ADMIN, ALL, ttl_seconds=TTL, clock=clock or Clock())
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def pair(db_url):
    clock = Clock()
    admin, worker = client(db_url, ADMIN, clock), client(db_url, WORKER, clock)
    assert admin.post("/jobs", json=JOB).status_code == 201
    return admin, worker


# --- store ---


def test_store_cancels_queued_and_leased_jobs(open_store):
    s = open_store()
    s.add("q", "a" * 64, "m", "v1", now=NOW)
    assert s.cancel("q") == "cancelled"
    assert s.get("q")["state"] == "cancelled"
    s.add("l", "b" * 64, "m", "v1", now=NOW)
    assert s.claim(NOW, TTL, **ANY) is not None
    assert s.cancel("l") == "cancelled"
    assert s.get("l")["state"] == "cancelled"


def test_store_never_leases_a_cancelled_job(open_store):
    s = open_store()
    s.add("a", "a" * 64, "m", "v1", now=NOW)
    s.cancel("a")
    assert s.claim(NOW, TTL, **ANY) is None
    # not even after the lease would have run out
    assert s.claim(NOW + 10_000, TTL, **ANY) is None


def test_store_cancelling_twice_is_already_not_an_error(open_store):
    s = open_store()
    s.add("a", "a" * 64, "m", "v1", now=NOW)
    assert s.cancel("a") == "cancelled"
    assert s.cancel("a") == "already"


def test_store_refuses_to_cancel_done_and_dead_jobs(open_store):
    s = open_store(max_attempts=1)
    s.add("d", "a" * 64, "m", "v1", now=NOW)
    claim = s.claim(NOW, TTL, **ANY)
    assert s.complete("d", claim.lease_token, KEY, now=NOW) is not None
    assert s.cancel("d") == "finished"
    assert s.get("d")["state"] == "done"
    s.add("x", "b" * 64, "m", "v1", now=NOW)
    s.claim(NOW, TTL, **ANY)
    assert s.claim(NOW + 2 * TTL, TTL, **ANY) is None  # attempts used up: dead-lettered
    assert s.get("x")["state"] == "dead"
    assert s.cancel("x") == "finished"
    assert s.get("x")["state"] == "dead"


def test_store_unknown_job_is_none(open_store):
    assert open_store().cancel("nope") is None


def test_a_cancelled_lease_cannot_heartbeat_or_complete_in_the_store(open_store):
    s = open_store()
    s.add("a", "a" * 64, "m", "v1", now=NOW)
    claim = s.claim(NOW, TTL, **ANY)
    s.cancel("a")
    assert s.heartbeat("a", claim.lease_token, NOW + 1, TTL) is None
    assert s.complete("a", claim.lease_token, KEY, now=NOW + 1) is None


def test_an_existing_database_gets_the_new_state_in_its_constraint(db_url):
    # A table made before this change has the old CHECK: opening the store must widen it.
    old_schema = store_module.SCHEMA.replace("'done', 'dead', 'cancelled'", "'done', 'dead'")
    assert old_schema != store_module.SCHEMA
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(old_schema)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO jobs (job_id, input_sha256, model, prompt_version, state)"
                " VALUES ('x', 'h', 'm', 'v', 'cancelled')"
            )
    s = JobStore(tracked_pool(db_url))
    s.add("a", "a" * 64, "m", "v1", now=NOW)
    assert s.cancel("a") == "cancelled"
    JobStore(tracked_pool(db_url))  # and a second start is a no-op


def test_a_second_start_leaves_the_widened_constraint_alone(db_url):
    # Dropping and re-adding it would take ACCESS EXCLUSIVE and re-scan the table on every
    # start: a new constraint has a new oid, so an unchanged oid means the migration skipped.
    query = "SELECT oid FROM pg_constraint WHERE conrelid = 'jobs'::regclass AND conname = %s"
    JobStore(tracked_pool(db_url))
    with psycopg.connect(db_url, autocommit=True) as conn:
        before = conn.execute(query, ("jobs_state_check",)).fetchone()
    assert before is not None
    JobStore(tracked_pool(db_url))
    with psycopg.connect(db_url, autocommit=True) as conn:
        assert conn.execute(query, ("jobs_state_check",)).fetchone() == before


# --- HTTP ---


def test_cancel_needs_the_admin_token(pair, db_url):
    _, worker = pair
    assert worker.post("/jobs/j/cancel").status_code == 401
    assert client(db_url, "x" * 40).post("/jobs/j/cancel").status_code == 401
    assert pair[0].post("/jobs/j/cancel").status_code == 200


def test_cancelling_a_queued_job_is_200_and_it_is_never_leased(pair):
    admin, worker = pair
    r = admin.post("/jobs/j/cancel")
    assert (r.status_code, r.json()) == (200, {"job_id": "j", "state": "cancelled"})
    assert worker.post("/lease", json=LEASE).status_code == 204


def test_cancel_is_idempotent_and_answers_200_again(pair):
    admin, _ = pair
    first, again = admin.post("/jobs/j/cancel"), admin.post("/jobs/j/cancel")
    assert (
        (again.status_code, again.json())
        == (first.status_code, first.json())
        == (
            200,
            {"job_id": "j", "state": "cancelled"},
        )
    )


def test_a_cancelled_lease_gets_409_on_heartbeat_and_complete(pair):
    admin, worker = pair
    leased = worker.post("/lease", json=LEASE).json()
    assert admin.post("/jobs/j/cancel").status_code == 200
    ref = {"job_id": "j", "lease_token": leased["lease_token"]}
    assert worker.post("/heartbeat", json=ref).status_code == 409
    assert worker.post("/complete", json={**ref, "result_key": KEY}).status_code == 409


def test_a_done_job_cannot_be_cancelled(pair):
    admin, worker = pair
    leased = worker.post("/lease", json=LEASE).json()
    ref = {"job_id": "j", "lease_token": leased["lease_token"]}
    assert worker.post("/complete", json={**ref, "result_key": KEY}).status_code == 204
    r = admin.post("/jobs/j/cancel")
    assert r.status_code == 409
    assert r.json()["code"] == "job_finished"
    # the worker's retry of its completion still works: the cancel changed nothing
    assert worker.post("/complete", json={**ref, "result_key": KEY}).status_code == 204


def test_a_dead_job_cannot_be_cancelled(db_url):
    clock = Clock()
    app = create_app(db_url, WORKER, ADMIN, ALL, ttl_seconds=TTL, clock=clock, max_attempts=1)
    admin, worker = (
        TestClient(app, headers={"Authorization": f"Bearer {t}"}) for t in (ADMIN, WORKER)
    )
    admin.post("/jobs", json=JOB)
    assert worker.post("/lease", json=LEASE).status_code == 200
    clock.now += 2 * TTL
    assert worker.post("/lease", json=LEASE).status_code == 204  # dead-lettered
    r = admin.post("/jobs/j/cancel")
    assert (r.status_code, r.json()["code"]) == (409, "job_finished")


def test_an_unknown_job_is_404(pair):
    r = pair[0].post("/jobs/nope/cancel")
    assert (r.status_code, r.json()["code"]) == (404, "job_not_found")


def test_a_malformed_job_id_is_422(pair):
    assert pair[0].post("/jobs/has space/cancel").status_code == 422


def test_cancelling_leaves_other_jobs_alone(pair):
    admin, worker = pair
    admin.post("/jobs", json={**JOB, "job_id": "k", "input_sha256": "b" * 64})
    admin.post("/jobs/j/cancel")
    assert worker.post("/lease", json=LEASE).json()["job_id"] == "k"
