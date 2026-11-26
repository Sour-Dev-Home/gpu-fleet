import json

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
    return HttpBroker(client, {"v1"}, {"m"}, clock=clock, allow_http=True)


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


@pytest.mark.parametrize("status", [429, 503])
def test_rate_limited_or_busy_is_retried_later(status):
    # 429: the broker's per-token rate limit (docs/security.md); 503: a busy database (ADR 0016).
    # Both mean "back off and try again", never "give up on the job".
    from fleet_worker.loop import TransientError

    clock = FakeClock()
    responses = {p: httpx.Response(status) for p in ("/lease", "/heartbeat", "/complete")}
    b = broker(responses, clock)
    with pytest.raises(TransientError):
        b.lease()
    with pytest.raises(TransientError):
        b.heartbeat(a_lease(received_at=1000.0))


def test_other_errors_are_not_mistaken_for_lease_lost():
    clock = FakeClock()
    b = broker({"/heartbeat": httpx.Response(401)}, clock)
    with pytest.raises(httpx.HTTPStatusError):
        b.heartbeat(a_lease(received_at=1000.0))


@pytest.mark.parametrize(
    "url",
    ["http://broker.example.com", "http://10.0.0.5:8000", "http://127.0.0.1.evil.com"],
)
def test_refuses_to_send_the_token_over_plain_http_to_a_remote_host(url):
    with pytest.raises(ValueError, match="refusing"):
        HttpBroker(httpx.Client(base_url=url), {"v1"}, {"m"})


@pytest.mark.parametrize(
    "url",
    [
        "https://broker.example.com",
        "http://127.0.0.1:8000",
        "http://127.0.0.2:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://[0:0:0:0:0:0:0:1]:8000",
    ],
)
def test_https_and_loopback_are_allowed(url):
    HttpBroker(httpx.Client(base_url=url), {"v1"}, {"m"})


# --- ADR 0015: the lease request declares what this worker can run ---


def test_lease_sends_capabilities_and_models():
    sent = []

    def handler(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    client = httpx.Client(base_url="http://broker", transport=httpx.MockTransport(handler))
    b = HttpBroker(
        client, {"smoke-v1", "echo-v1"}, {"qwen3:8b"}, clock=FakeClock(), allow_http=True
    )
    b.lease()
    path, body = sent[0]
    assert path == "/lease"
    assert body == {"capabilities": ["echo-v1", "smoke-v1"], "models": ["qwen3:8b"]}


@pytest.mark.parametrize("caps, models", [((), {"m"}), ({"v1"}, ())])
def test_declaring_nothing_is_refused_at_construction(caps, models):
    client = httpx.Client(base_url="http://127.0.0.1:8000")
    with pytest.raises(ValueError, match="at least one"):
        HttpBroker(client, caps, models)
