"""Storage trouble and results that are already stored (ADR 0025): the first valid result wins,
and a credentials or S3 outage answers 503 without costing a job an attempt."""

import hashlib

import pytest
from fleet_broker import s3
from test_app import CAPS, JOB, key_for
from test_presigned import (
    ADMIN,
    CAP,
    SHA,
    SIZE,
    FakeBucket,
    client,
    complete,
    enqueue,
    upload,
)


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


@pytest.fixture
def leased(db_url, bucket):
    """(worker client, the lease body) for job j0 with an S3 broker."""
    enqueue(client(db_url, bucket, ADMIN))
    w = client(db_url, bucket)
    return w, w.post("/lease", json=CAPS).json()


# --- a result that is already stored wins ---


def test_an_already_stored_result_is_recorded_and_no_url_is_issued(leased, bucket, open_store):
    w, lease = leased
    other = b"another run, other words"  # not what this worker would upload: still a valid result
    bucket.store(JOB, data=other)
    r = upload(w, lease)
    assert r.status_code == 200
    assert r.json() == {"already_stored": True}
    assert bucket.targets == []  # nothing to sign
    row = open_store().get("j0")
    assert (row["result_sha256"], row["result_size"]) == (
        hashlib.sha256(other).hexdigest(),
        len(other),
    )
    assert complete(w, lease).status_code == 204  # the stored object is what is declared


def test_a_stored_object_over_the_cap_is_not_adopted(leased, bucket):
    w, lease = leased
    bucket.objects[f"results/footage/{key_for(JOB)}"] = s3.ObjectInfo(CAP + 1, s3.b64_digest(SHA))
    assert "url" in upload(w, lease).json()


def test_a_stored_object_without_a_checksum_is_not_adopted(leased, bucket):
    w, lease = leased
    bucket.objects[f"results/footage/{key_for(JOB)}"] = s3.ObjectInfo(SIZE, None)
    assert "url" in upload(w, lease).json()


def test_a_stranger_cannot_learn_that_a_result_exists(leased, bucket):
    w, lease = leased
    bucket.store(JOB)
    assert upload(w, lease, lease_token="stale").status_code == 409
    assert bucket.heads == []  # the lease is checked before S3 is asked


def test_a_wrong_key_is_refused_before_s3_is_asked_on_upload(leased, bucket):
    w, lease = leased
    assert upload(w, lease, result_key="f" * 64).status_code == 422
    assert bucket.heads == []


# --- credentials are resolved before a job is claimed ---


def test_lease_with_credentials_down_is_503_and_claims_nothing(db_url, bucket, open_store):
    enqueue(client(db_url, bucket, ADMIN))
    bucket.credentials_down = True
    r = client(db_url, bucket).post("/lease", json=CAPS)
    assert r.status_code == 503
    assert r.headers["retry-after"]
    assert r.json()["code"] == "storage_unavailable"
    row = open_store().get("j0")
    assert (row["attempts"], row["state"]) == (0, "queued")  # no attempt burnt


def test_a_lease_works_again_when_credentials_come_back(db_url, bucket, open_store):
    enqueue(client(db_url, bucket, ADMIN))
    bucket.credentials_down = True
    w = client(db_url, bucket)
    w.post("/lease", json=CAPS)
    bucket.credentials_down = False
    assert w.post("/lease", json=CAPS).status_code == 200
    assert open_store().get("j0")["attempts"] == 1


def test_upload_url_with_credentials_down_is_503_and_declares_nothing(leased, bucket, open_store):
    w, lease = leased
    bucket.credentials_down = True
    r = upload(w, lease)
    assert (r.status_code, r.json()["code"]) == (503, "storage_unavailable")
    assert open_store().get("j0")["result_sha256"] is None


def test_a_presign_failure_on_lease_is_503_not_500(db_url, bucket):
    enqueue(client(db_url, bucket, ADMIN))
    bucket.presign_down = True
    r = client(db_url, bucket).post("/lease", json=CAPS)
    assert (r.status_code, r.json()["code"]) == (503, "storage_unavailable")


def test_a_real_presign_exception_leaks_nothing(db_url, caplog):
    class Boom:
        def generate_presigned_url(self, *a, **k):
            raise RuntimeError("secret-key-material-xyz")

    broken = s3.ResultBucket(Boom(), "b")
    enqueue(client(db_url, broken, ADMIN))
    r = client(db_url, broken).post("/lease", json=CAPS)
    assert r.status_code == 503
    assert r.json() == {"detail": "storage unavailable", "code": "storage_unavailable"}
    assert "secret-key-material-xyz" not in r.text + caplog.text


def test_a_real_presign_exception_on_upload_url_leaks_nothing(db_url, bucket, caplog):
    class Boom:
        def generate_presigned_url(self, *a, **k):
            raise RuntimeError("secret-key-material-xyz")

        def head_object(self, **k):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "Head"
            )

    enqueue(client(db_url, bucket, ADMIN))
    w = client(db_url, bucket)
    lease = w.post("/lease", json=CAPS).json()
    broken = client(db_url, s3.ResultBucket(Boom(), "b"))
    r = upload(broken, lease)
    assert (r.status_code, r.json()["code"]) == (503, "storage_unavailable")
    assert "secret-key-material-xyz" not in r.text + caplog.text


def test_a_head_that_cannot_answer_on_complete_is_storage_unavailable(leased, bucket):
    w, lease = leased
    upload(w, lease)
    bucket.unavailable = True
    r = complete(w, lease)
    assert (r.status_code, r.json()["code"]) == (503, "storage_unavailable")
