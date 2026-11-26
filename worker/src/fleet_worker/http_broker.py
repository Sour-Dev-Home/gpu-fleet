"""The worker's side of the broker API: implements the Broker protocol over HTTP."""

import ipaddress
import time
from collections.abc import Callable, Collection
from dataclasses import replace

import httpx

from . import telemetry
from .loop import LeaseLost, ResultRejected, TransientError
from .protocol import Job, Lease


def _is_loopback_ip(host: str) -> bool:
    """127.0.0.0/8 and ::1 in any spelling; hostnames like 127.0.0.1.evil.com are not IPs."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class HttpBroker:
    """Talks to the broker. `client` carries the base URL and the bearer token.

    Example:
        client = httpx.Client(base_url="http://127.0.0.1:8000",
                              headers={"Authorization": f"Bearer {token}"}, timeout=10)
        broker = HttpBroker(client)
    """

    def __init__(
        self,
        client: httpx.Client,
        capabilities: Collection[str],
        models: Collection[str],
        clock: Callable[[], float] = time.monotonic,
        allow_http: bool = False,
    ):
        # What this worker can run, sent with every lease request (ADR 0015). The broker
        # leases only matching jobs, and answers 422 to an empty list.
        if not capabilities or not models:
            raise ValueError("a worker must declare at least one prompt version and one model")
        self.capabilities = sorted(capabilities)
        self.models = sorted(models)
        # The bearer token rides on every request: never send it in cleartext to a remote
        # host. Plain http is fine for loopback, or when a test explicitly allows it.
        url = client.base_url
        loopback = url.host == "localhost" or _is_loopback_ip(url.host)
        if url.scheme != "https" and not (loopback or allow_http):
            raise ValueError(f"refusing to send the worker token over {url.scheme}://{url.host}")
        self.client = client
        self.clock = clock

    def _post(self, path: str, **kwargs) -> httpx.Response:
        """POST, turning "the broker is down, failing or busy" into TransientError (retry later).
        Other 4xx answers are returned as they are: they mean something specific (ADR 0007).
        429 (rate limited) is the exception: like a 503, it only means "not now"."""
        try:
            r = self.client.post(path, headers=telemetry.inject({}), **kwargs)
        except httpx.TransportError as e:  # connection refused, timeout, DNS, reset
            raise TransientError(f"{path}: {e!r}") from e
        if r.status_code >= 500 or r.status_code == 429:
            raise TransientError(f"{path}: HTTP {r.status_code}")
        return r

    def lease(self) -> Lease | None:
        # Stamp BEFORE sending. The broker starts the TTL when it handles the request,
        # which is after this moment, so the worker's idea of the expiry is never later
        # than the broker's. Stamping on arrival would err the unsafe way by a round trip.
        sent_at = self.clock()
        r = self._post("/lease", json={"capabilities": self.capabilities, "models": self.models})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        body = r.json()
        job = Job(
            body["job_id"],
            body["input_sha256"],
            body["model"],
            body["prompt_version"],
            body.get("context_sha256"),  # .get: brokers from before ADR 0012 omit it
        )
        return Lease(job, body["lease_token"], body["ttl_seconds"], received_at=sent_at)

    def heartbeat(self, lease: Lease) -> Lease:
        sent_at = self.clock()
        r = self._post(
            "/heartbeat",
            json={"job_id": lease.job.job_id, "lease_token": lease.lease_token},
        )
        if r.status_code == 409:
            raise LeaseLost(lease.job.job_id)
        r.raise_for_status()
        # A copy, since Lease is frozen, stamped with the time the request was SENT.
        return replace(lease, ttl_seconds=r.json()["ttl_seconds"], received_at=sent_at)

    def complete(self, lease: Lease, result_key: str) -> None:
        r = self._post(
            "/complete",
            json={
                "job_id": lease.job.job_id,
                "lease_token": lease.lease_token,
                "result_key": result_key,
            },
        )
        if r.status_code == 409:
            # The result is already stored under its content key, so nothing is lost:
            # whoever holds the job now will find it and complete cheaply.
            raise LeaseLost(lease.job.job_id)
        if r.status_code == 422:
            # The broker recomputed the content address and it differs (ADR 0023). Retrying
            # sends the same key, and the lease is not lost: its own outcome.
            raise ResultRejected(lease.job.job_id)
        r.raise_for_status()
