"""The background heartbeat (ADR 0014): a runner that blocks still keeps its lease.

These use real time with sub-second TTLs, so each test takes well under a second.
"""

import time

import httpx
import pytest
from fakes import FakeBroker, FakeStore
from fleet_worker.loop import LeaseLost, TransientError, Worker
from fleet_worker.protocol import Job, Lease

JOB = Job("j1", "a" * 64, "m", "v1")
TTL = 0.3  # renewal due after 0.2 s


def setup(runner, poll=0.02):
    clock, store = time.monotonic, FakeStore()
    broker = FakeBroker([Lease(JOB, "t", TTL, clock())], store, clock)
    worker = Worker(broker, store, runner, clock, heartbeat_poll_seconds=poll)
    return worker, broker, store


def blocking(seconds):
    def runner(job, keepalive):  # never calls keepalive, like one long HTTP request
        time.sleep(seconds)
        return b"done"

    return runner


def test_a_blocking_runner_keeps_its_lease():
    worker, broker, _ = setup(blocking(1.0))  # more than 3 TTLs, no keepalive calls
    assert worker.run_once() == "ran"
    assert len(broker.heartbeats) >= 3
    assert len(broker.completed) == 1


def test_a_quick_runner_sends_no_heartbeat():
    worker, broker, _ = setup(blocking(0.0))
    worker.run_once()
    assert broker.heartbeats == []


def test_lost_lease_keeps_the_result_but_does_not_complete():
    worker, broker, store = setup(blocking(0.5))
    broker.refuse_heartbeats = True
    with pytest.raises(LeaseLost):
        worker.run_once()
    assert store.data[JOB.result_key] == b"done"  # whoever holds the job now finds it
    assert broker.completed == []


def test_a_transient_heartbeat_failure_is_retried():
    worker, broker, _ = setup(blocking(0.6))
    real = broker.heartbeat
    calls = []

    def flaky(lease):
        calls.append(1)
        if len(calls) == 1:
            raise TransientError("broker restarting")
        return real(lease)

    broker.heartbeat = flaky
    assert worker.run_once() == "ran"
    assert len(calls) >= 2


def test_a_fatal_heartbeat_error_stops_the_worker():
    worker, broker, store = setup(blocking(0.5))

    def unauthorized(lease):
        raise httpx.HTTPStatusError("401", request=None, response=None)

    broker.heartbeat = unauthorized
    with pytest.raises(httpx.HTTPStatusError):
        worker.run_once()
    assert store.data == {}  # not a lost lease: nothing to hand over
