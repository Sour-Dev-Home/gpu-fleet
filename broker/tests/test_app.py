from fastapi.testclient import TestClient
from fleet_broker.app import create_app

TOKEN = "test-worker-token"  # a test value, not a secret
JOB = {
    "job_id": "j0",
    "input_sha256": "a" * 64,
    "model": "qwen3-vl:8b",
    "prompt_version": "v1",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def client(tmp_path, clock=None, token=TOKEN) -> TestClient:
    app = create_app(tmp_path / "broker.db", TOKEN, ttl_seconds=60, clock=clock or Clock())
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_missing_or_wrong_token_is_401(tmp_path):
    assert client(tmp_path, token="wrong").post("/lease").status_code == 401
    c = client(tmp_path)
    c.headers.pop("Authorization")
    assert c.post("/lease").status_code == 401


def test_empty_queue_is_204(tmp_path):
    assert client(tmp_path).post("/lease").status_code == 204


def test_lease_sends_a_duration_not_a_deadline(tmp_path):
    c = client(tmp_path)
    assert c.post("/jobs", json=JOB).status_code == 201
    body = c.post("/lease").json()
    assert body["job_id"] == "j0"
    assert body["ttl_seconds"] == 60
    assert "lease_expires_at" not in body  # the broker's clock never leaves the broker


def test_heartbeat_and_complete_with_a_stale_token_are_409(tmp_path):
    c = client(tmp_path)
    c.post("/jobs", json=JOB)
    c.post("/lease")
    stale = {"job_id": "j0", "lease_token": "not-the-token"}
    assert c.post("/heartbeat", json=stale).status_code == 409
    assert c.post("/complete", json=stale | {"result_key": "f" * 64}).status_code == 409


def test_complete_is_idempotent_over_http(tmp_path):
    c = client(tmp_path)
    c.post("/jobs", json=JOB)
    lease = c.post("/lease").json()
    done = {"job_id": "j0", "lease_token": lease["lease_token"], "result_key": "f" * 64}
    assert c.post("/complete", json=done).status_code == 204
    assert c.post("/complete", json=done).status_code == 204  # retry after a lost response
