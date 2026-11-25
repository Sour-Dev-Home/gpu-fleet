"""The phase 0 worker loop against the real broker API (in process, over HTTP semantics)."""

import pytest
from fastapi.testclient import TestClient
from fleet_broker.app import create_app
from fleet_broker.store import JobStore
from fleet_worker.http_broker import HttpBroker
from fleet_worker.loop import LeaseLost, Worker
from fleet_worker.protocol import result_key

# test values, not secrets
WORKER = "test-worker-token-0123456789abcdefghij"
ADMIN = "test-admin-token-0123456789abcdefghijk"


class Clock:
    """One fake clock for both sides: the broker reads it as wall time, the worker as
    monotonic time. Only the differences matter to either side."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DictStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return key in self.data

    def put(self, key: str, data: bytes) -> None:
        self.data[key] = data


@pytest.fixture
def fleet(tmp_path):
    clock = Clock()
    app = create_app(tmp_path / "broker.db", WORKER, ADMIN, ttl_seconds=60, clock=clock)
    http = TestClient(app, headers={"Authorization": f"Bearer {WORKER}"})
    admin = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
    store = DictStore()

    def add(job_id: str, input_sha256: str = "a" * 64) -> None:
        job = {
            "job_id": job_id,
            "input_sha256": input_sha256,
            "model": "m",
            "prompt_version": "v1",
        }
        assert admin.post("/jobs", json=job).status_code == 201

    def worker(runner) -> Worker:
        # TestClient's base URL is http://testserver, an in-process app with no network
        return Worker(HttpBroker(http, clock=clock, allow_http=True), store, runner, clock)

    def row(job_id: str):
        s = JobStore(tmp_path / "broker.db")
        try:
            return s.get(job_id)
        finally:
            s.close()

    return clock, add, worker, store, row


def quick(job, keepalive) -> bytes:
    return b"description"


def test_no_work(fleet):
    _, _, worker, _, _ = fleet
    assert worker(quick).run_once() is None


def test_job_runs_end_to_end(fleet):
    _, add, worker, store, row = fleet
    add("j0")
    assert worker(quick).run_once() == "ran"
    assert row("j0")["state"] == "done"
    assert store.data[row("j0")["result_key"]] == b"description"


def test_same_content_under_a_new_id_is_served_from_the_cache(fleet):
    _, add, worker, _, row = fleet
    add("j0")
    worker(quick).run_once()
    add("j1")  # same input, model and prompt: same result key
    assert worker(quick).run_once() == "cached"
    assert row("j1")["state"] == "done"


def test_long_job_heartbeats_and_completes(fleet):
    clock, add, worker, _, row = fleet

    def slow(job, keepalive):
        for _ in range(4):  # 4 x 45 s = 3 minutes on a 60 s lease
            clock.advance(45)
            keepalive()
        return b"description"

    add("j0")
    assert worker(slow).run_once() == "ran"
    assert row("j0")["state"] == "done"
    assert row("j0")["attempts"] == 1  # never expired, never re-leased


def test_worker_that_stalls_loses_the_job_to_another(fleet):
    clock, add, worker, _, row = fleet
    second = worker(quick)

    def stalls(job, keepalive):
        clock.advance(61)  # missed its renewal window entirely
        assert second.run_once() == "ran"  # another worker takes the job and finishes
        keepalive()  # too late
        return b"never stored"

    add("j0")
    with pytest.raises(LeaseLost):
        worker(stalls).run_once()
    assert row("j0")["state"] == "done"
    assert row("j0")["attempts"] == 2


def test_context_reaches_the_worker_and_keys_its_result(tmp_path):
    clock = Clock()
    app = create_app(tmp_path / "broker.db", WORKER, ADMIN, ttl_seconds=60, clock=clock)
    admin = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
    http = TestClient(app, headers={"Authorization": f"Bearer {WORKER}"})
    ctx = "c" * 64
    job = {"job_id": "p2", "input_sha256": "a" * 64, "model": "m", "prompt_version": "v1"}
    assert admin.post("/jobs", json=job | {"context_sha256": ctx}).status_code == 201

    store = DictStore()
    seen = []

    def runner(job, keepalive):
        seen.append(job.context_sha256)
        return b"description"

    Worker(HttpBroker(http, clock=clock, allow_http=True), store, runner, clock).run_once()
    assert seen == [ctx]
    assert list(store.data) == [result_key("a" * 64, "m", "v1", ctx)]
