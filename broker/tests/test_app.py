import logging
import time
import traceback
from contextlib import ExitStack
from urllib.parse import urlsplit, urlunsplit

import fleet_broker.app
import psycopg
import pytest
from fastapi.testclient import TestClient
from fleet_broker.app import create_app
from fleet_broker.store import JobStore
from tokens import ADMIN, WORKER

ALL = {"footage", "derived", "open"}
CAPS = {"capabilities": ["v1"], "models": ["qwen3-vl:8b"]}
JOB = {
    "job_id": "j0",
    "input_sha256": "a" * 64,
    "model": "qwen3-vl:8b",
    "prompt_version": "v1",
    "data_class": "footage",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def app(db_url, **kw):
    return create_app(db_url, WORKER, ADMIN, ALL, ttl_seconds=60, clock=Clock(), **kw)


def as_(token: str | None, db_url, **kw) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app(db_url, **kw), headers=headers)


@pytest.fixture
def worker(db_url) -> TestClient:
    as_(ADMIN, db_url).post("/jobs", json=JOB)
    return as_(WORKER, db_url)


# --- auth ---


def test_missing_or_wrong_token_is_401(db_url):
    assert as_("wrong", db_url).post("/lease", json=CAPS).status_code == 401
    assert as_(None, db_url).post("/lease", json=CAPS).status_code == 401


def test_workers_cannot_add_jobs(db_url):
    assert as_(WORKER, db_url).post("/jobs", json=JOB).status_code == 401
    assert as_(ADMIN, db_url).post("/jobs", json=JOB).status_code == 201


def test_admin_token_is_not_a_worker_token(db_url):
    assert as_(ADMIN, db_url).post("/lease", json=CAPS).status_code == 401


def test_non_ascii_auth_header_is_401_not_500(db_url):
    # raw bytes 0x80-0xFF, as an attacker's client would send them
    c = TestClient(app(db_url), raise_server_exceptions=False)
    assert (
        c.post("/lease", json=CAPS, headers={"Authorization": b"Bearer caf\xe9"}).status_code == 401
    )


@pytest.mark.parametrize(
    "worker_token, admin_token", [("short", ADMIN), (WORKER, "short"), (WORKER, WORKER)]
)
def test_weak_or_shared_tokens_are_refused_at_startup(db_url, worker_token, admin_token):
    with pytest.raises(ValueError):
        create_app(db_url, worker_token, admin_token, ALL)


OLD_WORKER = "old-worker-token-0123456789abcdefghijk"
OLD_ADMIN = "old-admin-token-0123456789abcdefghijkl"


def test_during_a_rotation_the_previous_tokens_still_work(db_url):
    # docs/secrets.md "Rotating broker tokens": workers move to the new token one at a time.
    rot = {"previous_worker_token": OLD_WORKER, "previous_admin_token": OLD_ADMIN}
    for token in (WORKER, OLD_WORKER):
        assert as_(token, db_url, **rot).post("/lease", json=CAPS).status_code == 204
    for token in (ADMIN, OLD_ADMIN):
        assert as_(token, db_url, **rot).post("/jobs", json=JOB).status_code == 201
    # Each role still accepts only its own tokens.
    assert as_(OLD_ADMIN, db_url, **rot).post("/lease", json=CAPS).status_code == 401
    assert as_(OLD_WORKER, db_url, **rot).post("/jobs", json=JOB).status_code == 401
    # After the rotation (no previous token configured) the old one is refused.
    assert as_(OLD_WORKER, db_url).post("/lease", json=CAPS).status_code == 401


@pytest.mark.parametrize(
    "rot",
    [
        {"previous_worker_token": "short"},
        {"previous_admin_token": "short"},
        {"previous_worker_token": ADMIN},  # an admin token must never pass as a worker token
        {"previous_admin_token": WORKER},
        {"previous_worker_token": OLD_WORKER, "previous_admin_token": OLD_WORKER},
    ],
)
def test_previous_tokens_get_the_same_checks(db_url, rot):
    with pytest.raises(ValueError):
        create_app(db_url, WORKER, ADMIN, ALL, **rot)


def test_worker_requests_are_rate_limited(db_url):
    # docs/security.md, before phase 4: a leaked token or a hot-looping worker can't run up the
    # Cloud Run bill or starve the thread pool. A token bucket: `burst` at once, then `rate`/s.
    clock = Clock()
    a = create_app(db_url, WORKER, ADMIN, ALL, clock=clock, worker_rate=1, worker_burst=2)
    w = TestClient(a, headers={"Authorization": f"Bearer {WORKER}"})
    assert [w.post("/lease", json=CAPS).status_code for _ in range(2)] == [204, 204]
    limited = w.post("/lease", json=CAPS)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "1"
    clock.now += 1  # one token back
    assert w.post("/lease", json=CAPS).status_code == 204
    assert w.post("/lease", json=CAPS).status_code == 429


def test_a_lease_hot_loop_cannot_cost_running_jobs_their_heartbeats(db_url):
    # Heartbeats have their own bucket: polling as fast as possible empties only the poll bucket.
    a = create_app(db_url, WORKER, ADMIN, ALL, clock=Clock(), worker_burst=2)
    w = TestClient(a, headers={"Authorization": f"Bearer {WORKER}"})
    assert [w.post("/lease", json=CAPS).status_code for _ in range(3)] == [204, 204, 429]
    beat = w.post("/heartbeat", json={"job_id": "j", "lease_token": "t"})
    assert beat.status_code == 409  # judged on its merits (no such lease), not rate limited


def test_strangers_cannot_use_up_the_workers_allowance(db_url):
    # Authentication comes first: a wrong token gets 401 without touching the bucket.
    a = create_app(db_url, WORKER, ADMIN, ALL, clock=Clock(), worker_burst=2)
    stranger = TestClient(a, headers={"Authorization": "Bearer " + "x" * 40})
    assert {stranger.post("/lease", json=CAPS).status_code for _ in range(10)} == {401}
    w = TestClient(a, headers={"Authorization": f"Bearer {WORKER}"})
    assert w.post("/lease", json=CAPS).status_code == 204


def test_the_admin_can_enqueue_in_bursts(db_url):
    a = create_app(db_url, WORKER, ADMIN, ALL, clock=Clock(), worker_burst=1)
    admin = TestClient(a, headers={"Authorization": f"Bearer {ADMIN}"})
    codes = {admin.post("/jobs", json=JOB | {"job_id": f"j{i}"}).status_code for i in range(5)}
    assert codes == {201}


# --- exposure and input limits ---


def test_api_docs_are_off_by_default(db_url):
    c = as_(None, db_url)
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
    assert as_(None, db_url, docs=True).get("/openapi.json").status_code == 200


@pytest.mark.parametrize(
    "field, value",
    [
        ("input_sha256", "not-a-hash"),
        ("input_sha256", "A" * 64),  # uppercase hex: one input must have one spelling
        ("job_id", "x" * 201),
        ("model", "bad name; DROP TABLE"),
    ],
)
def test_malformed_job_fields_are_422(db_url, field, value):
    c = as_(ADMIN, db_url)
    assert c.post("/jobs", json=JOB | {field: value}).status_code == 422


@pytest.mark.parametrize(
    "model", ["qwen3-vl:8b", "llama3.1:8b-instruct-q4_K_M", "hf.co/org/model:Q4", "m@sha256:abc"]
)
def test_real_model_names_are_accepted(db_url, model):
    assert as_(ADMIN, db_url).post("/jobs", json=JOB | {"model": model}).status_code == 201


def test_non_ascii_tokens_are_refused_at_startup(db_url):
    with pytest.raises(ValueError, match="ASCII"):
        create_app(db_url, WORKER, ADMIN[:-1] + "\xe9", ALL)


def test_unknown_fields_are_rejected(db_url):
    assert as_(ADMIN, db_url).post("/jobs", json=JOB | {"extra": 1}).status_code == 422


def test_result_key_must_be_a_sha256(worker):
    lease = worker.post("/lease", json=CAPS).json()
    done = {"job_id": "j0", "lease_token": lease["lease_token"], "result_key": "x" * 5000}
    assert worker.post("/complete", json=done).status_code == 422


def test_oversized_body_is_413(db_url):
    big = JOB | {"model": "m" * 20_000}
    assert as_(ADMIN, db_url).post("/jobs", json=big).status_code == 413


def test_an_oversized_chunked_body_is_413(db_url):
    # No Content-Length (chunked transfer): the size is only known while reading. Without a cap,
    # one request could make the broker buffer up to Cloud Run's 32 MiB.
    def chunks():
        for _ in range(20):
            yield b"x" * 1024  # 20 KiB in all, over the 16 KiB limit

    r = as_(ADMIN, db_url).post(
        "/jobs", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413


def test_a_small_chunked_body_still_works(db_url):
    import json

    def chunks():
        yield json.dumps(JOB).encode()

    r = as_(ADMIN, db_url).post(
        "/jobs", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 201


# --- protocol ---


def test_empty_queue_is_204(db_url):
    assert as_(WORKER, db_url).post("/lease", json=CAPS).status_code == 204


def test_lease_sends_a_duration_not_a_deadline(worker):
    body = worker.post("/lease", json=CAPS).json()
    assert body["job_id"] == "j0"
    assert body["ttl_seconds"] == 60
    assert "lease_expires_at" not in body  # the broker's clock never leaves the broker


def test_heartbeat_and_complete_with_a_stale_token_are_409(worker):
    worker.post("/lease", json=CAPS)
    stale = {"job_id": "j0", "lease_token": "not-the-token"}
    assert worker.post("/heartbeat", json=stale).status_code == 409
    assert worker.post("/complete", json=stale | {"result_key": "f" * 64}).status_code == 409


def test_complete_is_idempotent_over_http(worker):
    lease = worker.post("/lease", json=CAPS).json()
    done = {"job_id": "j0", "lease_token": lease["lease_token"], "result_key": "f" * 64}
    assert worker.post("/complete", json=done).status_code == 204
    assert worker.post("/complete", json=done).status_code == 204  # retry after a lost response


def test_context_must_be_a_sha256_when_given(db_url):
    c = as_(ADMIN, db_url)
    assert c.post("/jobs", json=JOB | {"context_sha256": "story bible"}).status_code == 422
    assert c.post("/jobs", json=JOB | {"context_sha256": "c" * 64}).status_code == 201


def test_healthz_needs_no_token_and_reveals_nothing(db_url):
    r = as_(None, db_url).get("/healthz")
    assert r.status_code == 204
    assert r.content == b""


# --- database unavailable is 503 (ADR 0020, ADR 0016) ---


def admin_client(db_url, **kw) -> TestClient:
    return TestClient(
        app(db_url, **kw),
        headers={"Authorization": f"Bearer {ADMIN}"},
        raise_server_exceptions=False,
    )


def hold_every_connection(client) -> ExitStack:
    pool = client.app.state.pool
    held = ExitStack()
    for _ in range(pool.max_size):
        held.enter_context(pool.connection())
    return held


def test_pool_timeout_is_503_with_retry_after(db_url, caplog):
    c = admin_client(db_url, pool_timeout=0.2)
    with hold_every_connection(c):  # four requests in flight, a fifth finds nothing free
        r = c.post("/jobs", json=JOB)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert "database unavailable" in caplog.text
    assert c.post("/jobs", json=JOB).status_code == 201  # recovers once connections are back


def test_a_dead_database_is_503_with_retry_after_and_recovers(db_url, admin_url):
    c = admin_client(db_url, pool_timeout=0.3)
    assert c.post("/jobs", json=JOB).status_code == 201  # the pool now holds a live connection
    name = db_url.rsplit("/", 1)[1]
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')  # "down for maintenance"
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
        )
        r = c.post("/jobs", json=JOB | {"job_id": "j1"})
        assert r.status_code == 503
        assert r.headers["retry-after"] == "1"
        admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
    # The pool backs off between reconnect attempts, so the first answers can still be 503
    # (which is why the contract is Retry-After); within seconds it is back.
    for _ in range(40):
        if c.post("/jobs", json=JOB | {"job_id": "j2"}).status_code == 201:
            break
        time.sleep(0.25)
    else:
        pytest.fail("the broker did not recover after the database came back")


def test_a_lock_timeout_is_503(db_url):
    # A real one: another session holds the table, the broker's lock_timeout (3 s) gives up.
    c = admin_client(db_url)
    with psycopg.connect(db_url) as holder:
        holder.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
        r = c.post("/jobs", json=JOB)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("connection refused"),  # no SQLSTATE: the client side
        psycopg.errors.ConnectionFailure("connection lost"),  # class 08
        psycopg.errors.QueryCanceled("statement timeout"),
        psycopg.errors.LockNotAvailable("lock timeout"),
        psycopg.errors.AdminShutdown("restarting"),
        psycopg.errors.CannotConnectNow("starting up"),
        psycopg.errors.TooManyConnections("too many"),
        psycopg.errors.DeadlockDetected("deadlock"),  # 40P01: transient, the retry succeeds
        psycopg.errors.SerializationFailure("serialization"),  # 40001
    ],
    ids=lambda e: type(e).__name__,
)
def test_connection_statement_and_lock_failures_are_503(db_url, monkeypatch, error):
    def boom(self, *a, **k):
        raise error

    monkeypatch.setattr(JobStore, "add", boom)
    r = admin_client(db_url).post("/jobs", json=JOB)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.UndefinedTable("relation jobs does not exist"),  # a bug, not weather
        psycopg.errors.DiskFull("disk full"),  # an operational error, but waiting won't fix it
        psycopg.errors.UniqueViolation("duplicate"),
        RuntimeError("anything else"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_other_errors_stay_500(db_url, monkeypatch, error):
    def boom(self, *a, **k):
        raise error

    monkeypatch.setattr(JobStore, "add", boom)
    r = admin_client(db_url).post("/jobs", json=JOB)
    assert r.status_code == 500
    assert "retry-after" not in r.headers


def test_the_password_never_reaches_an_exception_or_a_log(db_url, caplog):
    # A URL with a password the server rejects: startup fails, and the failure names no secret.
    secret = "hunter2-hunter2"  # a made-up marker, not a credential
    bad = urlsplit(db_url)
    bad = urlunsplit(bad._replace(netloc=f"nobody_here:{secret}@{bad.hostname}:{bad.port}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(psycopg.Error) as e:
        create_app(bad, WORKER, ADMIN, ALL, pool_timeout=0.2)
    text = caplog.text + str(e.value) + repr(e.value) + "".join(traceback.format_exception(e.value))
    assert secret not in text
    assert bad not in text


def test_a_failed_startup_leaves_no_pool_open(db_url, monkeypatch):
    opened = []
    real = fleet_broker.app.make_pool

    def spy(*a, **k):
        opened.append(pool := real(*a, **k))
        return pool

    def broken(self, *a, **k):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(fleet_broker.app, "make_pool", spy)
    monkeypatch.setattr(JobStore, "migrate", broken)
    with pytest.raises(RuntimeError, match="migration failed"):
        app(db_url)
    assert opened[0].closed
