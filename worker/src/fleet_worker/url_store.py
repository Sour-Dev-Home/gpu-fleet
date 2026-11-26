"""A ResultStore for one lease whose objects live in S3, reached by presigned URLs (ADR 0025).

The worker holds no cloud credentials: the broker signs a GET for the input (and context) in the
lease, and a PUT for the result on request (/upload-url). This module uses them with plain HTTPS.

  fetch_inputs()  download, hash while streaming, and compare with the job's input_sha256 (and
                  context_sha256). Bytes that differ are never run: the broker's bucket is not
                  trusted blindly, the job's content address is.
  put(key, data)  ask the broker for a PUT URL signed for these bytes' SHA-256 and length, send
                  the bytes with the headers it returns. If a result is already stored, the
                  broker says so instead and nothing is sent: the first valid result wins.

Every URL must be https and point at exactly FLEET_RESULTS_HOST, the bucket's own host, configured
on the worker and never taken from the broker. A compromised or spoofed broker could otherwise send
the worker to an internal address (the cloud metadata server, a router) with a "presigned URL".
Nothing loosens this, FLEET_ALLOW_HTTP included.

The HTTP client given here must NOT carry the broker's bearer token: presigned URLs are for S3,
and the token must never leave for another host (cli.build makes a separate client).
"""

import hashlib
import logging
import random
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from . import telemetry
from .loop import InputMismatch, InputMissing, TransientError, UploadRefused, retry_transient
from .protocol import Lease, UploadTarget

log = logging.getLogger(__name__)


class UploadBroker(Protocol):
    def upload_url(
        self, lease: Lease, result_key: str, sha256: str, size: int
    ) -> UploadTarget | None:
        """A PUT target, or None when the broker found a result already stored."""


def check_url(url: str, results_host: str) -> None:
    """Refuse anything but https to exactly `results_host` on the default port, without
    credentials in the URL. Raises ValueError (a misconfiguration or an attack, not a retry)."""
    if not results_host:
        raise ValueError("refusing a presigned URL: FLEET_RESULTS_HOST is not set")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or (parts.hostname or "") != results_host.lower()  # hostname is already lower-cased
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
    ):
        # The URL itself is a credential: say what was wrong, never what it was.
        raise ValueError(
            "refusing a presigned URL that is not https to the configured bucket host "
            "(FLEET_RESULTS_HOST)"
        )


def _unusable(r: httpx.Response, what: str) -> None:
    if r.status_code >= 500 or r.status_code in (409, 429):
        raise TransientError(f"{what}: HTTP {r.status_code}")


class UrlStore:
    def __init__(
        self,
        broker: UploadBroker,
        lease: Lease,
        http: httpx.Client,
        results_host: str,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        metrics: telemetry.Metrics | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.metrics = metrics or telemetry.Metrics()
        self._clock = clock
        self.broker = broker
        self.lease = lease
        self.http = http
        self.results_host = results_host.strip().lower()
        self._sleep, self._rng = sleep, rng

    def _download_and_hash(self, url: str, what: str) -> str:
        check_url(url, self.results_host)
        digest = hashlib.sha256()
        started = self._clock()
        try:
            with self.http.stream("GET", url) as r:
                _unusable(r, what)
                if r.status_code != 200:
                    # 404 (the role may list the bucket, so a missing key is a real 404) or 403
                    # (an expired URL): not something a retry fixes
                    raise InputMissing(
                        f"{self.lease.job.job_id}: {what} not available (HTTP {r.status_code})"
                    )
                for chunk in r.iter_bytes():
                    digest.update(chunk)
        except httpx.TransportError as e:
            # The type only: an httpx error can quote the URL, which is a credential.
            raise TransientError(f"{what}: {type(e).__name__}") from None
        self.metrics.transfer(self._clock() - started, "download")  # successes only
        return digest.hexdigest()

    def fetch_inputs(self) -> None:
        job = self.lease.job
        expected = [("input", self.lease.input_url, job.input_sha256)]
        if job.context_sha256 is not None:
            expected.append(("context", self.lease.context_url, job.context_sha256))
        for what, url, sha256 in expected:
            if url is None:
                raise InputMissing(f"{job.job_id}: the job has a {what} but the lease has no URL")
            # Retried here, not by the Worker: this runs inside its heartbeat thread, which keeps
            # the lease alive through the waits.
            got = retry_transient(
                lambda url=url, what=what: self._download_and_hash(url, what),
                self._sleep,
                self._rng,
            )
            if got != sha256:
                raise InputMismatch(
                    f"{job.job_id}: the downloaded {what} does not hash to the job's {what}_sha256"
                )

    def exists(self, key: str) -> bool:
        # No presigned GET for results, so a stored result cannot be seen from here. put() asks
        # the broker, which can: run again, and the broker adopts what is already stored.
        return False

    def _target(self, key: str, sha256: str, size: int) -> UploadTarget | None:
        target = self.broker.upload_url(self.lease, key, sha256, size)
        if target is not None:
            check_url(target.url, self.results_host)
        return target

    def put(self, key: str, data: bytes) -> None:
        sha256 = hashlib.sha256(data).hexdigest()
        target = self._target(key, sha256, len(data))
        if target is None:
            # A result is already stored (an earlier attempt). The first valid one wins: results
            # are content-addressed, so it is as good as ours, and /complete verifies it.
            log.info("result already stored", extra={"job_id": self.lease.job.job_id})
            return
        started = self._clock()
        try:
            r = self.http.put(target.url, content=data, headers=target.headers)
        except httpx.TransportError as e:
            raise TransientError(f"result upload: {type(e).__name__}") from None
        if r.is_success:
            self.metrics.transfer(self._clock() - started, "upload")
        if r.status_code == 412:
            # If-None-Match: * and the object appeared since the broker looked. Ask again: it
            # adopts the stored object, or says it cannot (no checksum, over the cap).
            if self._target(key, sha256, len(data)) is None:
                return
            raise UploadRefused(
                f"{self.lease.job.job_id}: a result is stored that the broker will not accept"
            )
        _unusable(r, "result upload")
        if not r.is_success:
            raise UploadRefused(
                f"{self.lease.job.job_id}: S3 refused the result upload (HTTP {r.status_code})"
            )
