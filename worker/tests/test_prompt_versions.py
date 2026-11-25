"""Runners own their prompt version (ADR 0014 amendment, 2026-11-25).

A runner declares the prompt versions it implements. A job for any other version means the
fleet is misconfigured (wrong FLEET_RUNNER, or jobs enqueued for another runner): the worker
stops loudly, before touching the store, and completes nothing.
"""

import pytest
from fakes import FakeBroker, FakeClock, FakeStore
from fleet_worker.backoff import Backoff
from fleet_worker.cli import echo
from fleet_worker.loop import Worker
from fleet_worker.protocol import Job, Lease

SMOKE_JOB = Job("j1", "a" * 64, "qwen3:8b", "smoke-v1")
ECHO_JOB = Job("j2", "b" * 64, "qwen3:8b", "echo-v1")


def worker_for(runner, *jobs: Job):
    clock, store = FakeClock(), FakeStore()
    broker = FakeBroker([Lease(j, "t", 60, clock()) for j in jobs], store, clock)
    return Worker(broker, store, runner, clock), broker, store


def test_echo_implements_echo_v1_only():
    assert echo.prompt_versions == frozenset({"echo-v1"})


def test_echo_worker_refuses_a_smoke_job_and_stores_and_completes_nothing():
    worker, broker, store = worker_for(echo, SMOKE_JOB)
    with pytest.raises(SystemExit, match="smoke-v1") as e:
        worker.run_once()
    assert "echo-v1" in str(e.value.code)  # names both versions: the job's and the runner's
    assert store.puts == 0
    assert broker.completed == []


def test_a_mismatch_is_caught_before_the_store_is_checked():
    # A stored result under this key must not be "reused" either: the job is misrouted.
    worker, broker, store = worker_for(echo, SMOKE_JOB)
    store.data[SMOKE_JOB.result_key] = b"written by smoke"
    with pytest.raises(SystemExit):
        worker.run_once()
    assert broker.completed == []


def test_a_matching_job_runs():
    worker, broker, _ = worker_for(echo, ECHO_JOB)
    assert worker.run_once() == "ran"
    assert [key for _, key in broker.completed] == [ECHO_JOB.result_key]


def test_run_stops_on_a_mismatch_instead_of_moving_on():
    worker, broker, _ = worker_for(echo, ECHO_JOB, SMOKE_JOB, ECHO_JOB)
    polls = iter(range(10))  # a bound, so a worker that moves on ends the test instead of hanging
    with pytest.raises(SystemExit, match="smoke-v1"):
        worker.run(
            should_stop=lambda: next(polls, None) is None,
            sleep=lambda s: None,
            backoff=Backoff(rng=lambda: 0.0),
        )
    assert len(broker.completed) == 1  # the first echo job; nothing after the mismatch
    assert len(broker.leases) == 1  # the third job was never leased


def test_a_runner_that_declares_no_prompt_versions_is_refused():
    with pytest.raises(TypeError, match="prompt_versions"):
        worker_for(lambda job, keepalive: b"x")


def test_echo_declares_the_echo_model_only():
    assert echo.models == frozenset({"echo"})
