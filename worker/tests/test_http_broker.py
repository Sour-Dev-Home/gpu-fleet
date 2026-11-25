import httpx
import pytest
from fakes import FakeClock
from fleet_worker.http_broker import HttpBroker
from fleet_worker.loop import LeaseLost
from fleet_worker.protocol import Job, Lease

LEASE_BODY = {
    "job_id": "j0",
    "input_sha256": "a" * 64,
    "model": "m",
    "prompt_version": "v1",
    "lease_token": "t",
    "ttl_seconds": 60,
    "attempts": 1,
}
LATENCY = 5.0  # seconds the fake network takes per request


def broker(responses: dict[str, httpx.Response], clock: FakeClock) -> HttpBroker:
    def handler(request: httpx.Request) -> httpx.Response:
        clock.advance(LATENCY)  # the broker handles the request after it was sent
        return responses[request.url.path]

    client = httpx.Client(base_url="http://broker", transport=httpx.MockTransport(handler))
    return HttpBroker(client, clock=clock)


def a_lease(received_at: float) -> Lease:
    return Lease(Job("j0", "a" * 64, "m", "v1"), "t", 60, received_at)


def test_empty_queue_is_none():
    clock = FakeClock()
    assert broker({"/lease": httpx.Response(204)}, clock).lease() is None


def test_lease_is_stamped_when_the_request_was_sent():
    clock = FakeClock(1000.0)
    lease = broker({"/lease": httpx.Response(200, json=LEASE_BODY)}, clock).lease()
    # stamped before the 5 s round trip: never later than the broker's own start
    assert lease.received_at == 1000.0
    assert lease.ttl_seconds == 60


def test_heartbeat_renews_and_is_stamped_when_sent():
    clock = FakeClock(1040.0)
    b = broker({"/heartbeat": httpx.Response(200, json={"ttl_seconds": 60})}, clock)
    renewed = b.heartbeat(a_lease(received_at=1000.0))
    assert renewed.received_at == 1040.0
    assert renewed.lease_token == "t"


def test_heartbeat_409_is_lease_lost():
    clock = FakeClock()
    b = broker({"/heartbeat": httpx.Response(409)}, clock)
    with pytest.raises(LeaseLost):
        b.heartbeat(a_lease(received_at=1000.0))


def test_other_errors_are_not_mistaken_for_lease_lost():
    clock = FakeClock()
    b = broker({"/heartbeat": httpx.Response(401)}, clock)
    with pytest.raises(httpx.HTTPStatusError):
        b.heartbeat(a_lease(received_at=1000.0))
