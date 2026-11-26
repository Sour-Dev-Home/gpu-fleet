"""The broker's S3 side (ADR 0025): presigned URLs, the HEAD check, the env switch and the
credentials from web identity federation. No network and no database: presigning is local
(the signature is computed from the credentials), HEAD answers come from botocore's Stubber, and
Google's metadata server and STS are fakes."""

import base64
import datetime as dt
import hashlib
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from fleet_broker import federation, s3

A = "a" * 64
BUCKET = "fleet-results-test"


def make_client():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
        aws_secret_access_key="fake",
        config=s3.client_config(),
    )


def bucket(client=None) -> s3.ResultBucket:
    return s3.ResultBucket(client or make_client(), BUCKET)


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


# --- presigned GET ---


def test_get_url_is_for_the_exact_object_and_lasts_ten_minutes():
    url = bucket().get_url(f"inputs/{A}")
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.hostname == f"{BUCKET}.s3.amazonaws.com"
    assert parts.path == f"/inputs/{A}"
    assert query(url)["X-Amz-Expires"] == "600"


# --- presigned PUT ---


def test_both_url_lifetimes_stay_inside_the_credential_refresh_window():
    # A URL dies with the credentials that signed it, and botocore renews them 10-15 minutes
    # before expiry: a URL signed just before a renewal must not outlive that margin.
    assert s3.GET_SECONDS <= 600 and s3.PUT_SECONDS <= 600
    b = bucket()
    assert query(b.get_url("inputs/x"))["X-Amz-Expires"] == str(s3.GET_SECONDS)
    assert query(b.put_target("results/x", "b" * 64, 1).url)["X-Amz-Expires"] == str(s3.PUT_SECONDS)


def test_put_target_signs_length_checksum_and_no_overwrite():
    sha = hashlib.sha256(b"hello").hexdigest()
    target = bucket().put_target(f"results/{A}", sha, 5)
    q = query(target.url)
    assert urlsplit(target.url).path == f"/results/{A}"
    assert q["X-Amz-Expires"] == "300"
    assert target.expires_in == 300
    signed = set(q["X-Amz-SignedHeaders"].split(";"))
    # the three the worker must honour: exact length, exact content, and "only if absent"
    assert {"content-length", "x-amz-checksum-sha256", "if-none-match"} <= signed
    assert signed <= {"host", "content-length", "x-amz-checksum-sha256", "if-none-match"}
    # the checksum header is base64 of the digest BYTES, not of the hex text
    assert (
        target.headers["x-amz-checksum-sha256"]
        == base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    )
    assert target.headers["If-None-Match"] == "*"


def test_put_target_signature_depends_on_the_declared_length_and_digest():
    sha = hashlib.sha256(b"hello").hexdigest()
    other = hashlib.sha256(b"other").hexdigest()
    one = query(bucket().put_target(f"results/{A}", sha, 5).url)["X-Amz-Signature"]
    longer = query(bucket().put_target(f"results/{A}", sha, 6).url)["X-Amz-Signature"]
    changed = query(bucket().put_target(f"results/{A}", other, 5).url)["X-Amz-Signature"]
    assert len({one, longer, changed}) == 3  # each of them is in the signature


def test_put_target_returns_every_signed_header_except_host_and_length():
    # a header that is signed but not sent makes S3 refuse the PUT: the list must match
    target = bucket().put_target(f"results/{A}", "b" * 64, 3)
    signed = set(query(target.url)["X-Amz-SignedHeaders"].split(";")) - {"host", "content-length"}
    assert {h.lower() for h in target.headers} == signed


# --- HEAD ---


def head_response(size: int, checksum: str | None):
    resp = {"ContentLength": size}
    if checksum is not None:
        resp["ChecksumSHA256"] = checksum
    return resp


def test_head_asks_for_the_checksum_and_returns_size_and_checksum():
    client = make_client()
    with Stubber(client) as stub:
        stub.add_response(
            "head_object",
            head_response(7, b64("c" * 64)),
            {"Bucket": BUCKET, "Key": f"results/{A}", "ChecksumMode": "ENABLED"},
        )
        info = bucket(client).head(f"results/{A}")
    assert info == s3.ObjectInfo(size=7, checksum_sha256=b64("c" * 64))


def test_head_of_an_object_without_a_checksum_says_so():
    client = make_client()
    with Stubber(client) as stub:
        stub.add_response("head_object", head_response(7, None))
        info = bucket(client).head(f"results/{A}")
    assert info.checksum_sha256 is None


@pytest.mark.parametrize("status", ["404", "403"])
def test_a_missing_object_is_none_whether_s3_says_404_or_403(status):
    # Without s3:ListBucket, S3 answers 403 for a key that does not exist (ADR 0025).
    client = make_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_object", http_status_code=int(status), service_error_code=status
        )
        assert bucket(client).head(f"results/{A}") is None


def test_any_other_s3_failure_is_unavailable_not_missing():
    client = make_client()
    with Stubber(client) as stub:
        stub.add_client_error("head_object", http_status_code=500, service_error_code="500")
        with pytest.raises(s3.BucketUnavailable):
            bucket(client).head(f"results/{A}")


# --- the environment switch ---


FULL = {
    "FLEET_RESULTS_BUCKET": BUCKET,
    "FLEET_AWS_ROLE_ARN": "arn:aws:iam::000000000000:role/fleet-broker",
    "FLEET_AWS_AUDIENCE": "fleet-aws-federation",
    "AWS_REGION": "us-east-1",
}


def fake_credentials(**kwargs):
    return None  # the client is built without looking anything up


def test_no_s3_settings_means_local_mode():
    assert s3.bucket_from_env({}, make_credentials=fake_credentials) is None
    # an AWS_REGION from the machine alone is not a request for S3
    assert (
        s3.bucket_from_env({"AWS_REGION": "us-east-1"}, make_credentials=fake_credentials) is None
    )


@pytest.mark.parametrize(
    "missing", ["FLEET_RESULTS_BUCKET", "FLEET_AWS_ROLE_ARN", "FLEET_AWS_AUDIENCE", "AWS_REGION"]
)
def test_some_but_not_all_s3_settings_is_a_startup_error(missing):
    env = {k: v for k, v in FULL.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        s3.bucket_from_env(env, make_credentials=fake_credentials)


@pytest.mark.parametrize(
    "only", ["FLEET_RESULTS_BUCKET", "FLEET_AWS_ROLE_ARN", "FLEET_AWS_AUDIENCE"]
)
def test_a_single_s3_setting_is_a_startup_error(only):
    with pytest.raises(ValueError):
        s3.bucket_from_env({only: FULL[only]}, make_credentials=fake_credentials)


def test_all_settings_build_a_bucket_with_credentials_from_the_federation():
    seen = {}

    def make_credentials(**kwargs):
        seen.update(kwargs)

    b = s3.bucket_from_env(FULL, make_credentials=make_credentials)
    assert isinstance(b, s3.ResultBucket)
    assert seen == {
        "role_arn": FULL["FLEET_AWS_ROLE_ARN"],
        "audience": FULL["FLEET_AWS_AUDIENCE"],
        "region": "us-east-1",
    }


@pytest.mark.parametrize("value", ["0", "-5", "abc", "10000000000000"])
def test_max_result_bytes_is_range_checked(value):
    with pytest.raises(ValueError, match="FLEET_MAX_RESULT_BYTES"):
        s3.max_result_bytes_from_env({"FLEET_MAX_RESULT_BYTES": value})


def test_max_result_bytes_defaults_to_10_megabytes():
    assert s3.max_result_bytes_from_env({}) == 10_000_000
    assert s3.max_result_bytes_from_env({"FLEET_MAX_RESULT_BYTES": "5000"}) == 5000


# --- credentials by web identity federation ---


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_id_token_is_fetched_from_the_metadata_server_for_the_audience():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(b"the.id.token\n")

    token = federation.fetch_id_token("fleet aws&x", opener=opener)
    request, timeout = calls[0]
    assert token == "the.id.token"
    assert request.full_url == (
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
        "default/identity?audience=fleet+aws%26x"
    )
    assert request.get_header("Metadata-flavor") == "Google"  # urllib capitalises the key
    assert timeout <= 10


class FakeSts:
    def __init__(self, lifetime: float = 3600.0) -> None:
        self.calls: list[dict] = []
        self.lifetime = lifetime

    def assume_role_with_web_identity(self, **kwargs):
        self.calls.append(kwargs)
        expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self.lifetime)
        return {
            "Credentials": {
                "AccessKeyId": f"AKIA{len(self.calls)}",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": expiry,
            }
        }


def test_credentials_come_from_assume_role_with_web_identity():
    sts = FakeSts()
    creds = federation.make_credentials(
        role_arn="arn:aws:iam::000000000000:role/fleet-broker",
        audience="fleet-aws-federation",
        region="us-east-1",
        sts=sts,
        fetch=lambda audience: f"id-token-for-{audience}",
    )
    frozen = creds.get_frozen_credentials()
    assert (frozen.access_key, frozen.secret_key, frozen.token) == ("AKIA1", "secret", "token")
    assert sts.calls == [
        {
            "RoleArn": "arn:aws:iam::000000000000:role/fleet-broker",
            "RoleSessionName": "fleet-broker",
            "WebIdentityToken": "id-token-for-fleet-aws-federation",
            "DurationSeconds": 3600,
        }
    ]


def test_credentials_refresh_before_they_expire():
    sts = FakeSts(lifetime=60.0)  # inside botocore's refresh window: every use renews
    creds = federation.make_credentials(
        role_arn="arn:aws:iam::000000000000:role/fleet-broker",
        audience="a",
        region="us-east-1",
        sts=sts,
        fetch=lambda audience: "t",
    )
    first = creds.get_frozen_credentials().access_key
    second = creds.get_frozen_credentials().access_key
    assert (first, second) != ("AKIA1", "AKIA1") and len(sts.calls) >= 2


def test_long_lived_credentials_are_not_refreshed_on_every_use():
    sts = FakeSts(lifetime=3600.0)
    creds = federation.make_credentials(
        role_arn="r", audience="a", region="us-east-1", sts=sts, fetch=lambda audience: "t"
    )
    creds.get_frozen_credentials()
    creds.get_frozen_credentials()
    assert len(sts.calls) == 1


def test_the_sts_client_does_not_sign_its_call():
    # AssumeRoleWithWebIdentity is authenticated by the token; boto would otherwise look for
    # credentials to sign it with and fail on a machine that has none.
    client = federation.sts_client("us-east-1")
    assert client.meta.config.signature_version is not None  # UNSIGNED is a sentinel object
    assert isinstance(client.meta.config, Config)
    assert "UNSIGNED" in repr(client.meta.config.signature_version).upper()


def test_a_client_error_from_sts_propagates():
    class Failing:
        def assume_role_with_web_identity(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Assume")

    with pytest.raises(ClientError):
        federation.make_credentials(
            role_arn="r", audience="a", region="us-east-1", sts=Failing(), fetch=lambda a: "t"
        )
