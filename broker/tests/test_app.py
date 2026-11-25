import pytest
from fastapi.testclient import TestClient
from fleet_broker.app import create_app
from tokens import ADMIN, WORKER

JOB = {"job_id": "j0", "input_sha256": "a" * 64, "model": "qwen3-vl:8b", "prompt_version": "v1"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def app(tmp_path, **kw):
    return create_app(tmp_path / "broker.db", WORKER, ADMIN, ttl_seconds=60, clock=Clock(), **kw)


def as_(token: str | None, tmp_path, **kw) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app(tmp_path, **kw), headers=headers)


@pytest.fixture
def worker(tmp_path) -> TestClient:
    as_(ADMIN, tmp_path).post("/jobs", json=JOB)
    return as_(WORKER, tmp_path)


# --- auth ---


def test_missing_or_wrong_token_is_401(tmp_path):
    assert as_("wrong", tmp_path).post("/lease").status_code == 401
    assert as_(None, tmp_path).post("/lease").status_code == 401


def test_workers_cannot_add_jobs(tmp_path):
    assert as_(WORKER, tmp_path).post("/jobs", json=JOB).status_code == 401
    assert as_(ADMIN, tmp_path).post("/jobs", json=JOB).status_code == 201


def test_admin_token_is_not_a_worker_token(tmp_path):
    assert as_(ADMIN, tmp_path).post("/lease").status_code == 401


def test_non_ascii_auth_header_is_401_not_500(tmp_path):
    # raw bytes 0x80-0xFF, as an attacker's client would send them
    c = TestClient(app(tmp_path), raise_server_exceptions=False)
    assert c.post("/lease", headers={"Authorization": b"Bearer caf\xe9"}).status_code == 401


@pytest.mark.parametrize(
    "worker_token, admin_token", [("short", ADMIN), (WORKER, "short"), (WORKER, WORKER)]
)
def test_weak_or_shared_tokens_are_refused_at_startup(tmp_path, worker_token, admin_token):
    with pytest.raises(ValueError):
        create_app(tmp_path / "broker.db", worker_token, admin_token)


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
        create_app(tmp_path / "broker.db", WORKER, ADMIN[:-1] + "\xe9")


def test_unknown_fields_are_rejected(tmp_path):
    assert as_(ADMIN, tmp_path).post("/jobs", json=JOB | {"extra": 1}).status_code == 422


def test_result_key_must_be_a_sha256(worker):
    lease = worker.post("/lease").json()
    done = {"job_id": "j0", "lease_token": lease["lease_token"], "result_key": "x" * 5000}
    assert worker.post("/complete", json=done).status_code == 422


def test_oversized_body_is_413(tmp_path):
    big = JOB | {"model": "m" * 20_000}
    assert as_(ADMIN, tmp_path).post("/jobs", json=big).status_code == 413


# --- protocol ---


def test_empty_queue_is_204(tmp_path):
    assert as_(WORKER, tmp_path).post("/lease").status_code == 204


def test_lease_sends_a_duration_not_a_deadline(worker):
    body = worker.post("/lease").json()
    assert body["job_id"] == "j0"
    assert body["ttl_seconds"] == 60
    assert "lease_expires_at" not in body  # the broker's clock never leaves the broker


def test_heartbeat_and_complete_with_a_stale_token_are_409(worker):
    worker.post("/lease")
    stale = {"job_id": "j0", "lease_token": "not-the-token"}
    assert worker.post("/heartbeat", json=stale).status_code == 409
    assert worker.post("/complete", json=stale | {"result_key": "f" * 64}).status_code == 409


def test_complete_is_idempotent_over_http(worker):
    lease = worker.post("/lease").json()
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
