"""A ResultStore for one lease whose objects live in S3, reached by presigned URLs (ADR 0025).

The worker holds no cloud credentials: the broker signs a GET for the input (and context) in the
lease, and a PUT for the result on request (/upload-url). This module uses them with plain HTTPS.

  fetch_inputs()  download, hash while streaming, and compare with the job's input_sha256 (and
                  context_sha256). Bytes that differ are never run: the broker's bucket is not
                  trusted blindly, the job's content address is.
  put(key, data)  ask the broker for a PUT URL signed for these bytes' SHA-256 and length, send
                  the bytes with the headers it returns.

The HTTP client given here must NOT carry the broker's bearer token: presigned URLs are for S3,
and the token must never leave for another host (cli.build makes a separate client).
"""

import hashlib
import ipaddress
import logging
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .loop import InputMismatch, InputMissing, TransientError, UploadRefused
from .protocol import Lease, UploadTarget

log = logging.getLogger(__name__)


class UploadBroker(Protocol):
    def upload_url(self, lease: Lease, result_key: str, sha256: str, size: int) -> UploadTarget: ...


def _check_url(url: str, allow_http: bool) -> None:
    """https only: a presigned URL is a bearer credential. Loopback http is allowed for local
    S3 stand-ins (MinIO) and tests."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "https" and not (allow_http or (parts.scheme == "http" and loopback)):
        raise ValueError(
            f"refusing to use a presigned URL over {parts.scheme or 'no scheme'}, not https"
        )


def _unusable(r: httpx.Response, what: str) -> None:
    if r.status_code >= 500 or r.status_code in (409, 429):
        raise TransientError(f"{what}: HTTP {r.status_code}")


class UrlStore:
    def __init__(
        self, broker: UploadBroker, lease: Lease, http: httpx.Client, allow_http: bool = False
    ) -> None:
        self.broker = broker
        self.lease = lease
        self.http = http
        self.allow_http = allow_http

    def _download_and_hash(self, url: str, what: str) -> str:
        _check_url(url, self.allow_http)
        digest = hashlib.sha256()
        try:
            with self.http.stream("GET", url) as r:
                _unusable(r, what)
                if r.status_code != 200:
                    # S3 answers 403 for a key that does not exist (the broker has no ListBucket)
                    raise InputMissing(
                        f"{self.lease.job.job_id}: {what} not available (HTTP {r.status_code})"
                    )
                for chunk in r.iter_bytes():
                    digest.update(chunk)
        except httpx.TransportError as e:
            raise TransientError(f"{what}: {e!r}") from e
        return digest.hexdigest()

    def fetch_inputs(self) -> None:
        job = self.lease.job
        expected = [("input", self.lease.input_url, job.input_sha256)]
        if job.context_sha256 is not None:
            expected.append(("context", self.lease.context_url, job.context_sha256))
        for what, url, sha256 in expected:
            if url is None:
                raise InputMissing(f"{job.job_id}: the job has a {what} but the lease has no URL")
            if self._download_and_hash(url, what) != sha256:
                raise InputMismatch(
                    f"{job.job_id}: the downloaded {what} does not hash to the job's {what}_sha256"
                )

    def exists(self, key: str) -> bool:
        # No presigned GET for results, so a stored result cannot be seen from here: run again.
        # The upload's If-None-Match makes a repeat harmless (put treats 412 as stored).
        return False

    def put(self, key: str, data: bytes) -> None:
        sha256 = hashlib.sha256(data).hexdigest()
        target = self.broker.upload_url(self.lease, key, sha256, len(data))
        _check_url(target.url, self.allow_http)
        try:
            r = self.http.put(target.url, content=data, headers=target.headers)
        except httpx.TransportError as e:
            raise TransientError(f"result upload: {e!r}") from e
        if r.status_code == 412:
            # If-None-Match: * and the object is there: an earlier attempt stored it. /complete
            # compares it with this attempt's declaration, so differing bytes cannot slip through.
            log.info("result already stored", extra={"job_id": self.lease.job.job_id})
            return
        _unusable(r, "result upload")
        if not r.is_success:
            raise UploadRefused(
                f"{self.lease.job.job_id}: S3 refused the result upload (HTTP {r.status_code})"
            )
