import pytest
from fastapi.testclient import TestClient
from fleet_broker.app import create_app
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


def app(tmp_path, **kw):
    return create_app(
        tmp_path / "broker.db", WORKER, ADMIN, ALL, ttl_seconds=60, clock=Clock(), **kw
    )


def as_(token: str | None, tmp_path, **kw) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app(tmp_path, **kw), headers=headers)


@pytest.fixture
def worker(tmp_path) -> TestClient:
    as_(ADMIN, tmp_path).post("/jobs", json=JOB)
    return as_(WORKER, tmp_path)


# --- auth ---


def test_missing_or_wrong_token_is_401(tmp_path):
    assert as_("wrong", tmp_path).post("/lease", json=CAPS).status_code == 401
    assert as_(None, tmp_path).post("/lease", json=CAPS).status_code == 401


def test_workers_cannot_add_jobs(tmp_path):
    assert as_(WORKER, tmp_path).post("/jobs", json=JOB).status_code == 401
    assert as_(ADMIN, tmp_path).post("/jobs", json=JOB).status_code == 201


def test_admin_token_is_not_a_worker_token(tmp_path):
    assert as_(ADMIN, tmp_path).post("/lease", json=CAPS).status_code == 401


def test_non_ascii_auth_header_is_401_not_500(tmp_path):
    # raw bytes 0x80-0xFF, as an attacker's client would send them
    c = TestClient(app(tmp_path), raise_server_exceptions=False)
    assert (
        c.post("/lease", json=CAPS, headers={"Authorization": b"Bearer caf\xe9"}).status_code == 401
    )


@pytest.mark.parametrize(
    "worker_token, admin_token", [("short", ADMIN), (WORKER, "short"), (WORKER, WORKER)]
)
def test_weak_or_shared_tokens_are_refused_at_startup(tmp_path, worker_token, admin_token):
    with pytest.raises(ValueError):
        create_app(tmp_path / "broker.db", worker_token, admin_token, ALL)


OLD_WORKER = "old-worker-token-0123456789abcdefghijk"
OLD_ADMIN = "old-admin-token-0123456789abcdefghijkl"


def test_during_a_rotation_the_previous_tokens_still_work(tmp_path):
    # docs/secrets.md "Rotating broker tokens": workers move to the new token one at a time.
    rot = {"previous_worker_token": OLD_WORKER, "previous_admin_token": OLD_ADMIN}
    for token in (WORKER, OLD_WORKER):
        assert as_(token, tmp_path, **rot).post("/lease", json=CAPS).status_code == 204
    for token in (ADMIN, OLD_ADMIN):
        assert as_(token, tmp_path, **rot).post("/jobs", json=JOB).status_code == 201
    # Each role still accepts only its own tokens.
    assert as_(OLD_ADMIN, tmp_path, **rot).post("/lease", json=CAPS).status_code == 401
    assert as_(OLD_WORKER, tmp_path, **rot).post("/jobs", json=JOB).status_code == 401
    # After the rotation (no previous token configured) the old one is refused.
    assert as_(OLD_WORKER, tmp_path).post("/lease", json=CAPS).status_code == 401


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
def test_previous_tokens_get_the_same_checks(tmp_path, rot):
    with pytest.raises(ValueError):
        create_app(tmp_path / "broker.db", WORKER, ADMIN, ALL, **rot)


def test_worker_requests_are_rate_limited(tmp_path):
    # docs/security.md, before phase 4: a leaked token or a hot-looping worker can't run up the
    # Cloud Run bill or starve the thread pool. A token bucket: `burst` at once, then `rate`/s.
    clock = Clock()
    a = create_app(
        tmp_path / "b.db", WORKER, ADMIN, ALL, clock=clock, worker_rate=1, worker_burst=2
    )
    w = TestClient(a, headers={"Authorization": f"Bearer {WORKER}"})
    assert [w.post("/lease", json=CAPS).status_code for _ in range(2)] == [204, 204]
    limited = w.post("/lease", json=CAPS)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "1"
    clock.now += 1  # one token back
    assert w.post("/lease", json=CAPS).status_code == 204
    assert w.post("/lease", json=CAPS).status_code == 429


def test_a_lease_hot_loop_cannot_cost_running_jobs_their_heartbeats(tmp_path):
    # Heartbeats have their own bucket: polling as fast as possible empties only the poll bucket.
    a = create_app(tmp_path / "b.db", WORKER, ADMIN, ALL, clock=Clock(), worker_burst=2)
    w = TestClient(a, headers={"Authorization": f"Bearer {WORKER}"})
    assert [w.post("/lease", json=CAPS).status_code for _ in range(3)] == [204, 204, 429]
    beat = w.post("/heartbeat", json={"job_id": "j", "lease_token": "t"})
    assert beat.status_code == 409  # judged on its merits (no such lease), not rate limited


def test_strangers_cannot_use_up_the_workers_allowance(tmp_path):
    # Authentication comes first: a wrong token gets 401 without touching the bucket.
    a = create_app(tmp_path / "b.db", WORKER, ADMIN, ALL, clock=Clock(), worker_burst=2)
    stranger = TestClient(a, headers={"Authorization": "Bearer " + "x" * 40})
    assert {stranger.post("/lease", json=CAPS).status_code for _ in range(10)} == {401}
    w = TestClient(a, headers={"Authorization": f"Bearer {WORKER}"})
    assert w.post("/lease", json=CAPS).status_code == 204


def test_the_admin_can_enqueue_in_bursts(tmp_path):
    a = create_app(tmp_path / "b.db", WORKER, ADMIN, ALL, clock=Clock(), worker_burst=1)
    admin = TestClient(a, headers={"Authorization": f"Bearer {ADMIN}"})
    codes = {admin.post("/jobs", json=JOB | {"job_id": f"j{i}"}).status_code for i in range(5)}
    assert codes == {201}


# --- exposure and input limits ---


def test_api_docs_are_off_by_default(tmp_path):
    c = as_(None, tmp_path)
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
    assert as_(None, tmp_path, docs=True).get("/openapi.json").status_code == 200


@pytest.mark.parametrize(
    "field, value",
    [
        ("input_sha256", "not-a-hash"),
        ("input_sha256", "A" * 64),  # uppercase hex: one input must have one spelling
        ("job_id", "x" * 201),
        ("model", "bad name; DROP TABLE"),
    ],
)
def test_malformed_job_fields_are_422(tmp_path, field, value):
    c = as_(ADMIN, tmp_path)
    assert c.post("/jobs", json=JOB | {field: value}).status_code == 422


@pytest.mark.parametrize(
    "model", ["qwen3-vl:8b", "llama3.1:8b-instruct-q4_K_M", "hf.co/org/model:Q4", "m@sha256:abc"]
)
def test_real_model_names_are_accepted(tmp_path, model):
    assert as_(ADMIN, tmp_path).post("/jobs", json=JOB | {"model": model}).status_code == 201


def test_non_ascii_tokens_are_refused_at_startup(tmp_path):
    with pytest.raises(ValueError, match="ASCII"):
        create_app(tmp_path / "broker.db", WORKER, ADMIN[:-1] + "\xe9", ALL)


def test_unknown_fields_are_rejected(tmp_path):
    assert as_(ADMIN, tmp_path).post("/jobs", json=JOB | {"extra": 1}).status_code == 422


def test_result_key_must_be_a_sha256(worker):
    lease = worker.post("/lease", json=CAPS).json()
    done = {"job_id": "j0", "lease_token": lease["lease_token"], "result_key": "x" * 5000}
    assert worker.post("/complete", json=done).status_code == 422


def test_oversized_body_is_413(tmp_path):
    big = JOB | {"model": "m" * 20_000}
    assert as_(ADMIN, tmp_path).post("/jobs", json=big).status_code == 413


def test_an_oversized_chunked_body_is_413(tmp_path):
    # No Content-Length (chunked transfer): the size is only known while reading. Without a cap,
    # one request could make the broker buffer up to Cloud Run's 32 MiB.
    def chunks():
        for _ in range(20):
            yield b"x" * 1024  # 20 KiB in all, over the 16 KiB limit

    r = as_(ADMIN, tmp_path).post(
        "/jobs", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413


def test_a_small_chunked_body_still_works(tmp_path):
    import json

    def chunks():
        yield json.dumps(JOB).encode()

    r = as_(ADMIN, tmp_path).post(
        "/jobs", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 201


# --- protocol ---


def test_empty_queue_is_204(tmp_path):
    assert as_(WORKER, tmp_path).post("/lease", json=CAPS).status_code == 204


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


def test_context_must_be_a_sha256_when_given(tmp_path):
    c = as_(ADMIN, tmp_path)
    assert c.post("/jobs", json=JOB | {"context_sha256": "story bible"}).status_code == 422
    assert c.post("/jobs", json=JOB | {"context_sha256": "c" * 64}).status_code == 201


def test_healthz_needs_no_token_and_reveals_nothing(tmp_path):
    r = as_(None, tmp_path).get("/healthz")
    assert r.status_code == 204
    assert r.content == b""


# --- SQLite busy handling (ADR 0016) ---


def test_database_runs_in_wal_mode(tmp_path):
    import sqlite3

    app(tmp_path)
    mode = sqlite3.connect(tmp_path / "broker.db").execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_locked_database_is_503_with_retry_after(tmp_path, caplog):
    import sqlite3

    c = as_(ADMIN, tmp_path, busy_timeout=0.05)
    holder = sqlite3.connect(tmp_path / "broker.db", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        r = c.post("/jobs", json=JOB)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert "locked" in caplog.text
    assert c.post("/jobs", json=JOB).status_code == 201  # recovers once the lock is gone


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("no such table: jobs", 1),  # SQLITE_ERROR
        # SQLITE_LOCKED: a conflict inside one connection, a bug; retrying can't fix it
        ("database table is locked", 6),
    ],
)
def test_other_operational_errors_stay_500(tmp_path, monkeypatch, message, code):
    import sqlite3

    from fleet_broker.store import JobStore

    def boom(self, *a, **k):
        e = sqlite3.OperationalError(message)
        e.sqlite_errorcode = code
        raise e

    monkeypatch.setattr(JobStore, "add", boom)
    c = TestClient(
        app(tmp_path), headers={"Authorization": f"Bearer {ADMIN}"}, raise_server_exceptions=False
    )
    assert c.post("/jobs", json=JOB).status_code == 500
