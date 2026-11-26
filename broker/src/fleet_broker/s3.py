"""The broker's view of the results bucket: presigned URLs and one HEAD (ADR 0025).

Layout (one bucket, content-addressed, Q74, Q82):
    inputs/<data class>/<input sha256>    results/<data class>/<result key>
The data class comes from the job's row, never from the caller. The cloud broker's AWS role
may read and write only the `open` prefixes (infra/aws/fleet/broker_role.tf).

Workers never hold AWS credentials. The broker signs a URL per object, locally (no network:
the signature is computed from the credentials), and the worker uses it with plain HTTPS.
URLs are only ever built for keys the broker derives from a job's own row, never from a key the
caller names (app.py).
"""

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import federation

# A presigned URL dies with the credentials that signed it. Those last SESSION_SECONDS (federation.py,
# 3600) and botocore renews them 10-15 minutes before expiry, so a URL signed at the last moment still
# fits in its own lifetime only if it lasts no longer than that margin: keep both <= 600 (tested).
GET_SECONDS = 600  # a 10 minute window to start downloading an input
PUT_SECONDS = 300  # and 5 to start the upload; a transfer already begun is not cut off
DEFAULT_MAX_RESULT_BYTES = 10_000_000  # a description is kilobytes; 10 MB is a bug or an attack
MAX_RESULT_BYTES_LIMIT = 5 * 1024**3  # S3's single-PUT ceiling: a larger cap could never be used
DISABLED = "presigned_urls_disabled"  # the code of /upload-url's 404 in local mode
DATA_CLASSES = frozenset({"footage", "derived", "open"})  # GLOSSARY.md "Data class"
S3_ENV = ("FLEET_RESULTS_BUCKET", "FLEET_AWS_ROLE_ARN", "FLEET_AWS_AUDIENCE")


class BucketUnavailable(Exception):
    """S3 could not answer (network, 5xx, throttling, credentials): retry later."""


def _checked(data_class: str) -> str:
    if data_class not in DATA_CLASSES:  # the app's types already say so; a key must not trust that
        raise ValueError(f"unknown data class {data_class!r}")
    return data_class


def input_key(data_class: str, sha256: str) -> str:
    return f"inputs/{_checked(data_class)}/{sha256}"


def result_object_key(data_class: str, result_key: str) -> str:
    return f"results/{_checked(data_class)}/{result_key}"


def hex_digest(b64: str) -> str:
    """The inverse of b64_digest: S3 reports a checksum in base64, the job row keeps hex."""
    return base64.b64decode(b64).hex()


def b64_digest(sha256_hex: str) -> str:
    """S3 wants a checksum as base64 of the digest's 32 bytes, not of its hex text."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


@dataclass(frozen=True)
class UploadTarget:
    url: str
    headers: dict[str, str]  # exactly the signed headers (bar host, length): send them as given
    expires_in: int


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    checksum_sha256: str | None  # base64, as S3 reports it; None if uploaded without one


def client_config() -> Config:
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
        # botocore's default adds a CRC32 of its own to uploads; ours is the SHA-256 we sign.
        request_checksum_calculation="when_required",
        retries={"max_attempts": 2, "mode": "standard"},
        connect_timeout=3,
        read_timeout=5,
    )


class ResultBucket:
    def __init__(self, client: Any, bucket: str, credentials: Any = None) -> None:
        self.client = client
        self.bucket = bucket
        self.credentials = credentials  # the refreshing credentials the client signs with

    def check(self) -> None:
        """Resolve the signing credentials now (renewing them if due), so an STS or metadata
        outage surfaces BEFORE a job is claimed: a claim costs the job an attempt. Local
        when the credentials are fresh. Only the exception's type is kept."""
        if self.credentials is None:
            return
        try:
            self.credentials.get_frozen_credentials()
        except Exception as e:  # noqa: BLE001 - any failure to get credentials is "unavailable"
            raise BucketUnavailable(type(e).__name__) from None

    def get_url(self, key: str, expires: int = GET_SECONDS) -> str:
        try:
            return self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
            )
        except Exception as e:  # noqa: BLE001
            raise BucketUnavailable(type(e).__name__) from None

    def put_target(
        self, key: str, sha256_hex: str, size: int, expires: int = PUT_SECONDS
    ) -> UploadTarget:
        """A PUT URL that is signed for this exact length and SHA-256, and for "only if the key
        does not exist yet". The signed headers are returned for the worker to send.

        Whether S3 enforces each of them on a presigned PUT is checked by scripts/probe-presign.py;
        /complete verifies the stored object either way (HEAD)."""
        checksum = b64_digest(sha256_hex)
        try:
            url = self.client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentLength": size,
                    "ChecksumSHA256": checksum,
                    "IfNoneMatch": "*",
                },
                ExpiresIn=expires,
            )
        except Exception as e:  # noqa: BLE001
            raise BucketUnavailable(type(e).__name__) from None
        signed = _signed_headers(url)
        wanted = {"x-amz-checksum-sha256": checksum, "If-None-Match": "*"}
        # Only what the signature covers: an extra x-amz-* header would make S3 refuse the PUT.
        headers = {name: v for name, v in wanted.items() if name.lower() in signed}
        return UploadTarget(url=url, headers=headers, expires_in=expires)

    def head(self, key: str) -> ObjectInfo | None:
        """The stored object's size and SHA-256, or None if there is no such object."""
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as e:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:  # the role may list the bucket, so a missing key is a real 404
                return None
            # 403 included: the role or the bucket policy is wrong. Not "missing".
            raise BucketUnavailable(str(e.response.get("Error", {}).get("Code") or status)) from e
        except BotoCoreError as e:  # no connection, timeout, no credentials
            raise BucketUnavailable(type(e).__name__) from e
        return ObjectInfo(size=int(r["ContentLength"]), checksum_sha256=r.get("ChecksumSHA256"))


def _signed_headers(url: str) -> set[str]:
    names = parse_qs(urlsplit(url).query).get("X-Amz-SignedHeaders", [""])[0]
    return set(names.split(";")) - {"host", "content-length"}


def max_result_bytes_from_env(env: Mapping[str, str]) -> int:
    raw = env.get("FLEET_MAX_RESULT_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_RESULT_BYTES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 0 < value <= MAX_RESULT_BYTES_LIMIT:
        raise ValueError(f"FLEET_MAX_RESULT_BYTES must be 1..{MAX_RESULT_BYTES_LIMIT}, got {raw!r}")
    return value


def bucket_from_env(
    env: Mapping[str, str], make_credentials: Callable[..., Any] = federation.make_credentials
) -> ResultBucket | None:
    """None (local mode: no presigned URLs) when none of the S3 settings is set; a bucket when
    all are; a startup error for anything in between, so a half-set deployment cannot quietly
    run in the wrong mode. AWS_REGION alone does not count: developer machines set it."""
    given = {name: env.get(name, "").strip() for name in (*S3_ENV, "AWS_REGION")}
    if not any(given[name] for name in S3_ENV):
        return None
    missing = [name for name, value in given.items() if not value]
    if missing:
        raise ValueError(f"S3 mode needs all of {', '.join(given)}; missing {', '.join(missing)}")
    credentials = make_credentials(
        role_arn=given["FLEET_AWS_ROLE_ARN"],
        audience=given["FLEET_AWS_AUDIENCE"],
        region=given["AWS_REGION"],
    )
    session = _session(credentials)
    client = session.client("s3", region_name=given["AWS_REGION"], config=client_config())
    return ResultBucket(client, given["FLEET_RESULTS_BUCKET"], credentials)


def _session(credentials: Any) -> boto3.Session:
    core = botocore.session.Session()
    # Not public API, but the documented way to give a session self-refreshing credentials.
    core._credentials = credentials
    return boto3.Session(botocore_session=core)
