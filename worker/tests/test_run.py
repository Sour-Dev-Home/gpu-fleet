import httpx
import pytest
from fakes import FakeBroker, FakeClock, FakeStore, declares
from fleet_worker.backoff import Backoff
from fleet_worker.http_broker import HttpBroker
from fleet_worker.loop import InvalidResult, LeaseLost, TransientError, Worker
from fleet_worker.protocol import Job, Lease

JOB = Job("j1", "a" * 64, "m", "v1")


# --- Backoff ---


def test_backoff_doubles_up_to_the_cap():
    b = Backoff(base=1, cap=60, rng=lambda: 1.0)  # rng=1.0: always the full ceiling
    assert [b.next_delay() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]


def test_backoff_is_jittered_below_the_ceiling():
    b = Backoff(base=1, cap=60, rng=lambda: 0.25)
    assert [b.next_delay() for _ in range(3)] == [0.25, 0.5, 1.0]


def test_backoff_reset_starts_over():
    b = Backoff(base=1, cap=60, rng=lambda: 1.0)
    for _ in range(5):
        b.next_delay()
    b.reset()
    assert b.next_delay() == 1


def test_backoff_survives_a_very_long_outage():
    b = Backoff(base=1, cap=60, rng=lambda: 1.0)
    b.failures = 10_000  # 2**10000 would overflow a float
    assert b.next_delay() == 60


# --- run() ---


def make(leases, runner=lambda job, keepalive: b"ok"):
    clock, store = FakeClock(), FakeStore()
    broker = FakeBroker(leases, store, clock)
    return Worker(broker, store, declares(runner, "v1"), clock), broker, store


def lease_for(job: Job = JOB) -> Lease:
    return Lease(job, "t", 60, 1000.0)


def run_until(worker, sleeps_wanted: int):
    """Run until the worker has slept sleeps_wanted times; return the sleeps."""
    sleeps: list[float] = []
    worker.run(
        should_stop=lambda: len(sleeps) >= sleeps_wanted,
        sleep=sleeps.append,
        backoff=Backoff(base=1, cap=60, rng=lambda: 1.0),
    )
    return sleeps


def test_idle_worker_backs_off_exponentially():
    worker, _, _ = make([])
    assert run_until(worker, 4) == [1, 2, 4, 8]


def test_work_resets_the_backoff_and_is_taken_without_waiting():
    worker, broker, _ = make([])
    # idle, idle, two jobs back to back, idle, idle
    queue = [None, None, lease_for(), lease_for(Job("j2", "b" * 64, "m", "v1")), None, None]
    broker.lease = lambda: queue.pop(0) if queue else None
    # without the reset the waits would keep doubling: [1, 2, 4, 8]
    assert run_until(worker, 4) == [1, 2, 1, 2]
    assert len(broker.completed) == 2


def test_invalid_output_is_never_stored_or_completed():
    worker, broker, store = make([lease_for()], runner=lambda job, keepalive: b"   ")
    run_until(worker, 1)
    assert store.puts == 0
    assert broker.completed == []


def test_invalid_output_raises_from_process():
    worker, _, _ = make([lease_for()], runner=lambda job, keepalive: b"")
    with pytest.raises(InvalidResult):
        worker.run_once()


def test_custom_validator_is_used():
    def reject_all(job, data):
        raise InvalidResult("nope")

    worker, broker, _ = make([lease_for()])
    worker.validate = reject_all
    run_until(worker, 1)
    assert broker.completed == []


def test_a_refused_result_key_backs_off_and_the_worker_goes_on(caplog):
    from fleet_worker.loop import ResultRejected

    worker, broker, _ = make([])
    queue = [lease_for(), lease_for(Job("j2", "b" * 64, "m", "v1"))]
    sleeps: list[float] = []
    calls = []

    def next_lease():
        # Asking again before any sleep would be a spin; after the rejection it must wait first.
        if calls:
            assert sleeps == [1], "no backoff after a refused key"
        return queue.pop(0) if queue else None

    def refusing_complete(lease, key):
        calls.append(lease.job.job_id)
        if len(calls) == 1:
            raise ResultRejected(lease.job.job_id)
        broker.completed.append((lease, key))

    broker.lease, broker.complete = next_lease, refusing_complete
    worker.run(
        should_stop=lambda: len(calls) >= 2,
        sleep=sleeps.append,
        backoff=Backoff(base=1, cap=60, rng=lambda: 1.0),
    )
    assert calls == ["j1", "j2"]  # went on to the next job: not fatal, not "lease lost"
    assert "j1" in caplog.text


def test_lease_lost_does_not_stop_the_worker():
    worker, broker, _ = make([lease_for(), lease_for(Job("j2", "b" * 64, "m", "v1"))])
    calls = []

    def flaky_complete(lease, key):
        calls.append(lease.job.job_id)
        if len(calls) == 1:
            raise LeaseLost(lease.job.job_id)
        broker.completed.append((lease, key))

    broker.complete = flaky_complete
    run_until(worker, 1)
    assert calls == ["j1", "j2"]


def test_transient_errors_back_off_then_recover():
    worker, broker, _ = make([])
    attempts = []

    def down_then_up():
        attempts.append(1)
        if len(attempts) <= 2:
            raise TransientError("connection refused")

    broker.lease = down_then_up
    assert run_until(worker, 3) == [1, 2, 4]


def test_unexpected_errors_stop_the_worker():
    worker, broker, _ = make([])

    def unauthorized():
        raise httpx.HTTPStatusError("401", request=None, response=None)

    broker.lease = unauthorized
    with pytest.raises(httpx.HTTPStatusError):
        run_until(worker, 5)


# --- HttpBroker error mapping ---


def http_broker(handler) -> HttpBroker:
    client = httpx.Client(base_url="http://broker", transport=httpx.MockTransport(handler))
    return HttpBroker(client, {"v1"}, {"m"}, clock=FakeClock(), allow_http=True)


def test_5xx_is_transient():
    with pytest.raises(TransientError):
        http_broker(lambda request: httpx.Response(503)).lease()


def test_network_failure_is_transient():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransientError):
        http_broker(refuse).lease()


def test_401_is_not_transient():
    with pytest.raises(httpx.HTTPStatusError):
        http_broker(lambda request: httpx.Response(401)).lease()
