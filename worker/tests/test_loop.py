import pytest
from fakes import FakeBroker, FakeClock, FakeStore
from fleet_worker.loop import LeaseLost, Worker
from fleet_worker.protocol import Job, Lease

JOB = Job(
    job_id="j1",
    input_sha256="a" * 64,
    model="qwen3-vl:8b",
    prompt_version="describe-v1",
)


def setup(runner, ttl: float = 60.0):
    clock, store = FakeClock(), FakeStore()
    lease = Lease(job=JOB, lease_token="t", ttl_seconds=ttl, received_at=clock())
    broker = FakeBroker([lease], store, clock)
    return Worker(broker, store, runner, clock), broker, store, clock


def quick_runner(job, keepalive):
    return b"description"


def test_no_work_returns_none():
    worker, broker, _, _ = setup(quick_runner)
    broker.leases.clear()
    assert worker.run_once() is None


def test_runs_stores_under_result_key_and_completes():
    worker, broker, store, _ = setup(quick_runner)
    assert worker.run_once() == "ran"
    assert store.data == {JOB.result_key: b"description"}
    assert [key for _, key in broker.completed] == [JOB.result_key]


def test_existing_result_is_reused_without_running():
    calls = []
    worker, broker, store, _ = setup(lambda job, keepalive: calls.append(job) or b"new")
    store.data[JOB.result_key] = b"old"
    assert worker.run_once() == "cached"
    assert calls == []
    assert store.data[JOB.result_key] == b"old"
    assert [key for _, key in broker.completed] == [JOB.result_key]


def test_keepalive_heartbeats_only_when_due():
    def slow_runner(job, keepalive):
        clock.advance(10)
        keepalive()  # 10 s into a 60 s lease: not due
        clock.advance(35)
        keepalive()  # 45 s: due
        return b"description"

    worker, broker, _, clock = setup(slow_runner)
    worker.run_once()
    assert len(broker.heartbeats) == 1


def test_complete_uses_the_renewed_lease():
    def slow_runner(job, keepalive):
        clock.advance(45)
        keepalive()
        return b"description"

    worker, broker, _, clock = setup(slow_runner)
    worker.run_once()
    ((lease, _),) = broker.completed
    assert lease.lease_token == "t+"  # the token from the heartbeat, not the original


def test_lost_lease_stops_without_storing_or_completing():
    def slow_runner(job, keepalive):
        clock.advance(45)
        keepalive()
        return b"description"

    worker, broker, store, clock = setup(slow_runner)
    broker.refuse_heartbeats = True
    with pytest.raises(LeaseLost):
        worker.run_once()
    assert store.puts == 0
    assert broker.completed == []
