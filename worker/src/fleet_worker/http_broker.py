"""The worker's side of the broker API: implements the Broker protocol over HTTP."""

import time
from collections.abc import Callable
from dataclasses import replace

import httpx

from .loop import LeaseLost, TransientError
from .protocol import Job, Lease


class HttpBroker:
    """Talks to the broker. `client` carries the base URL and the bearer token.

    Example:
        client = httpx.Client(base_url="http://127.0.0.1:8000",
                              headers={"Authorization": f"Bearer {token}"}, timeout=10)
        broker = HttpBroker(client)
    """

    def __init__(self, client: httpx.Client, clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.clock = clock

    def _post(self, path: str, **kwargs) -> httpx.Response:
        """POST, turning "the broker is down or failing" into TransientError (retry later).
        4xx answers are returned as they are: they mean something specific (ADR 0007)."""
        try:
            r = self.client.post(path, **kwargs)
        except httpx.TransportError as e:  # connection refused, timeout, DNS, reset
            raise TransientError(f"{path}: {e!r}") from e
        if r.status_code >= 500:
            raise TransientError(f"{path}: HTTP {r.status_code}")
        return r

    def lease(self) -> Lease | None:
        # Stamp BEFORE sending. The broker starts the TTL when it handles the request,
        # which is after this moment, so the worker's idea of the expiry is never later
        # than the broker's. Stamping on arrival would err the unsafe way by a round trip.
        sent_at = self.clock()
        r = self._post("/lease")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        body = r.json()
        job = Job(body["job_id"], body["input_sha256"], body["model"], body["prompt_version"])
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
        r.raise_for_status()
