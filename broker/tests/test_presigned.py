"""Presigned URLs through the HTTP API (ADR 0025): who gets them, for which keys, and what
/complete insists on before it records a result. The bucket is a fake: the real signing and HEAD
are tested in test_s3.py."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from fleet_broker import s3
from fleet_broker.app import create_app
from test_app import ALL, CAPS, JOB, Clock, key_for
from tokens import ADMIN, WORKER

CAP = 1000  # a small result cap, so the tests need not build 10 MB
RESULT = b"a description"
SHA = hashlib.sha256(RESULT).hexdigest()
SIZE = len(RESULT)
CONTEXT = "c" * 64


class FakeBucket:
    """Records what the broker asks of S3 and answers from `objects` (key -> ObjectInfo)."""

    def __init__(self) -> None:
        self.objects: dict[str, s3.ObjectInfo] = {}
        self.heads: list[str] = []
        self.targets: list[tuple[str, str, int]] = []
        self.unavailable = False
        self.credentials_down = False  # the STS or metadata server cannot be reached
        self.presign_down = False  # signing itself raises

    def check(self) -> None:
        if self.credentials_down:
            raise s3.BucketUnavailable("RefreshError")

    def get_url(self, key: str, expires: int = 600) -> str:
        if self.presign_down:
            raise s3.BucketUnavailable("RuntimeError")
        return f"https://fake.s3/get/{key}?exp={expires}"

    def put_target(self, key: str, sha256_hex: str, size: int, expires: int = 300):
        self.targets.append((key, sha256_hex, size))
        return s3.UploadTarget(
            url=f"https://fake.s3/put/{key}",
            headers={"x-amz-checksum-sha256": s3.b64_digest(sha256_hex)},
            expires_in=expires,
        )

    def head(self, key: str):
        self.heads.append(key)
        if self.unavailable:
            raise s3.BucketUnavailable("500")
        return self.objects.get(key)

    def store(self, job: dict, data: bytes = RESULT, context: str | None = None) -> None:
        """Put `data` where a correct upload would have."""
        sha = hashlib.sha256(data).hexdigest()
        self.objects[f"results/{key_for(job, context)}"] = s3.ObjectInfo(
            len(data), s3.b64_digest(sha)
        )


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


def client(db_url, bucket, token=WORKER, **kw) -> TestClient:
    app = create_app(
        db_url,
        WORKER,
        ADMIN,
        ALL,
        ttl_seconds=60,
        clock=Clock(),
        bucket=bucket,
        **({"max_result_bytes": CAP} | kw),
    )
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def enqueue(c_admin: TestClient, **fields) -> dict:
    job = JOB | fields
    assert c_admin.post("/jobs", json=job).status_code == 201
    return job


@pytest.fixture
def leased(db_url, bucket):
    """(worker client, the leased job's lease body) for job j0, with an S3 broker."""
    enqueue(client(db_url, bucket, ADMIN))
    w = client(db_url, bucket)
    return w, w.post("/lease", json=CAPS).json()


def upload(w, lease, **over):
    body = {
        "job_id": lease["job_id"],
        "lease_token": lease["lease_token"],
        "result_key": key_for(JOB),
        "sha256": SHA,
        "size": SIZE,
    } | over
    return w.post("/upload-url", json=body)


def complete(w, lease, **over):
    body = {
        "job_id": lease["job_id"],
        "lease_token": lease["lease_token"],
        "result_key": key_for(JOB),
    } | over
    return w.post("/complete", json=body)


# --- /lease ---


def test_lease_carries_a_presigned_input_url_for_the_inputs_prefix(leased):
    _, lease = leased
    assert lease["input_url"] == f"https://fake.s3/get/inputs/{JOB['input_sha256']}?exp=600"
    assert "context_url" not in lease or lease["context_url"] is None


def test_lease_carries_a_context_url_when_the_job_has_context(db_url, bucket):
    enqueue(client(db_url, bucket, ADMIN), context_sha256=CONTEXT)
    lease = client(db_url, bucket).post("/lease", json=CAPS).json()
    assert lease["context_url"] == f"https://fake.s3/get/inputs/{CONTEXT}?exp=600"
    assert lease["input_url"].endswith(f"inputs/{JOB['input_sha256']}?exp=600")


@pytest.mark.parametrize("data_class", ["footage", "derived", "open"])
def test_urls_are_issued_for_every_data_class(db_url, bucket, data_class):
    # The clearance filter decides who may lease; whoever holds the lease gets its URLs (Q77).
    enqueue(client(db_url, bucket, ADMIN), data_class=data_class)
    assert client(db_url, bucket).post("/lease", json=CAPS).json()["input_url"]


def test_local_mode_lease_has_exactly_the_old_fields(db_url):
    enqueue(client(db_url, None, ADMIN))
    lease = client(db_url, None).post("/lease", json=CAPS).json()
    assert set(lease) == {
        "job_id",
        "input_sha256",
        "model",
        "prompt_version",
        "context_sha256",
        "lease_token",
        "ttl_seconds",
        "attempts",
    }


# --- /upload-url ---


def test_upload_url_is_for_the_results_prefix_and_the_jobs_own_key(leased, bucket):
    w, lease = leased
    r = upload(w, lease)
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == f"https://fake.s3/put/results/{key_for(JOB)}"
    assert body["headers"] == {"x-amz-checksum-sha256": s3.b64_digest(SHA)}
    assert body["expires_in"] == 300
    assert bucket.targets == [(f"results/{key_for(JOB)}", SHA, SIZE)]


def test_upload_url_for_a_job_with_context_uses_the_key_with_context(db_url, bucket):
    enqueue(client(db_url, bucket, ADMIN), context_sha256=CONTEXT)
    w = client(db_url, bucket)
    lease = w.post("/lease", json=CAPS).json()
    key = key_for(JOB, CONTEXT)
    assert upload(w, lease, result_key=key).json()["url"].endswith(f"results/{key}")
    assert upload(w, lease, result_key=key_for(JOB)).status_code == 422  # the context-free key


def test_upload_url_records_what_was_declared_on_the_job(leased, open_store):
    w, lease = leased
    upload(w, lease)
    row = open_store().get("j0")
    assert (row["result_sha256"], row["result_size"]) == (SHA, SIZE)


def test_upload_url_needs_the_worker_token(leased, db_url, bucket):
    _, lease = leased
    assert upload(client(db_url, bucket, ADMIN), lease).status_code == 401
    assert upload(TestClient(client(db_url, bucket).app), lease).status_code == 401


def test_upload_url_with_a_wrong_lease_token_is_409_and_signs_nothing(leased, bucket):
    w, lease = leased
    assert upload(w, lease, lease_token="not-the-token").status_code == 409
    assert bucket.targets == []


def test_someone_elses_job_gets_no_upload_url(db_url, bucket):
    # worker B holds j1; its valid token must not open j0, nor can j0's token open j1
    admin = client(db_url, bucket, ADMIN)
    enqueue(admin, job_id="j0")
    enqueue(admin, job_id="j1", input_sha256="b" * 64)
    w = client(db_url, bucket)
    first = w.post("/lease", json=CAPS).json()
    second = w.post("/lease", json=CAPS).json()
    assert {first["job_id"], second["job_id"]} == {"j0", "j1"}
    mine = upload(w, first, job_id=second["job_id"])  # first's token on second's job
    assert mine.status_code == 409
    assert bucket.targets == []


def test_a_queued_or_finished_job_gets_no_upload_url(db_url, bucket):
    enqueue(client(db_url, bucket, ADMIN))
    w = client(db_url, bucket)
    fake = {"job_id": "j0", "lease_token": "x" * 22}
    assert upload(w, fake).status_code == 409  # queued: nobody holds it
    lease = w.post("/lease", json=CAPS).json()
    upload(w, lease)
    bucket.store(JOB)
    assert complete(w, lease).status_code == 204
    assert upload(w, lease).status_code == 409  # done
    assert len(bucket.targets) == 1


def test_a_result_key_that_is_not_the_jobs_is_422_with_the_same_code_and_no_url(leased, bucket):
    w, lease = leased
    r = upload(w, lease, result_key="f" * 64)
    assert r.status_code == 422
    assert r.json()["code"] == "result_key_mismatch"
    assert bucket.targets == []


def test_an_oversize_result_is_413_and_no_url(leased, bucket):
    w, lease = leased
    assert upload(w, lease, size=CAP + 1).status_code == 413
    assert bucket.targets == []


def test_a_result_of_exactly_the_cap_is_allowed(leased):
    w, lease = leased
    assert upload(w, lease, size=CAP).status_code == 200


@pytest.mark.parametrize("over", [{"size": 0}, {"size": -1}, {"sha256": "A" * 64}, {"sha256": "x"}])
def test_malformed_upload_fields_are_422_without_the_mismatch_code(leased, over):
    w, lease = leased
    r = upload(w, lease, **over)
    assert r.status_code == 422
    assert "code" not in r.json()


def test_upload_url_in_local_mode_is_404_with_a_code(db_url):
    enqueue(client(db_url, None, ADMIN))
    w = client(db_url, None)
    lease = w.post("/lease", json=CAPS).json()
    r = upload(w, lease)
    assert r.status_code == 404
    assert r.json()["code"] == "presigned_urls_disabled"


def test_upload_url_in_local_mode_still_needs_the_token(db_url):
    assert (
        upload(client(db_url, None, "wrong"), {"job_id": "j0", "lease_token": "x"}).status_code
        == 401
    )


# --- /complete in S3 mode ---


def test_complete_accepts_a_stored_result_that_matches_the_declaration(leased, bucket, open_store):
    w, lease = leased
    upload(w, lease)
    bucket.store(JOB)
    assert complete(w, lease).status_code == 204
    assert bucket.heads == [f"results/{key_for(JOB)}"] * 2  # at /upload-url, then /complete
    assert open_store().get("j0")["state"] == "done"


def test_complete_with_a_missing_object_is_422_result_missing(leased, open_store):
    w, lease = leased
    upload(w, lease)  # declared, never uploaded
    r = complete(w, lease)
    assert r.status_code == 422
    assert r.json()["code"] == "result_missing"
    assert open_store().get("j0")["state"] == "leased"  # still the worker's


def test_complete_with_a_different_checksum_is_422_result_mismatch(leased, bucket, open_store):
    w, lease = leased
    upload(w, lease)
    bucket.store(
        JOB, data=b"x" * SIZE
    )  # the declared length, other bytes: only the checksum differs
    r = complete(w, lease)
    assert (r.status_code, r.json()["code"]) == (422, "result_mismatch")
    assert open_store().get("j0")["state"] == "leased"


def test_complete_with_a_stored_object_over_the_cap_is_422_result_mismatch(leased, bucket):
    w, lease = leased
    upload(w, lease)
    bucket.objects[f"results/{key_for(JOB)}"] = s3.ObjectInfo(CAP + 1, s3.b64_digest(SHA))
    assert complete(w, lease).json()["code"] == "result_mismatch"


def test_a_cap_lowered_after_the_upload_is_enforced_on_the_stored_object(db_url, bucket):
    enqueue(client(db_url, bucket, ADMIN))
    big = client(db_url, bucket, max_result_bytes=CAP * 2)
    lease = big.post("/lease", json=CAPS).json()
    sha = hashlib.sha256(b"y" * (CAP + 500)).hexdigest()
    assert upload(big, lease, sha256=sha, size=CAP + 500).status_code == 200  # fine under CAP*2
    bucket.objects[f"results/{key_for(JOB)}"] = s3.ObjectInfo(CAP + 500, s3.b64_digest(sha))
    small = client(db_url, bucket)  # the cap is CAP again after a redeploy
    assert complete(small, lease).json()["code"] == "result_mismatch"


def test_complete_with_a_different_length_is_422_result_mismatch(leased, bucket):
    w, lease = leased
    upload(w, lease)
    bucket.objects[f"results/{key_for(JOB)}"] = s3.ObjectInfo(SIZE + 1, s3.b64_digest(SHA))
    assert complete(w, lease).json()["code"] == "result_mismatch"


def test_complete_with_an_object_that_has_no_checksum_is_422_result_mismatch(leased, bucket):
    w, lease = leased
    upload(w, lease)
    bucket.objects[f"results/{key_for(JOB)}"] = s3.ObjectInfo(SIZE, None)
    assert complete(w, lease).json()["code"] == "result_mismatch"


def test_complete_without_any_upload_url_is_422_result_mismatch(leased, bucket):
    w, lease = leased
    bucket.store(JOB)  # an object is there, but this lease never declared it
    r = complete(w, lease)
    assert (r.status_code, r.json()["code"]) == (422, "result_mismatch")


def test_a_wrong_result_key_is_refused_before_s3_is_asked(leased, bucket):
    w, lease = leased
    upload(w, lease)
    bucket.heads.clear()  # /upload-url looked at S3 once; /complete must not
    r = complete(w, lease, result_key="f" * 64)
    assert (r.status_code, r.json()["code"]) == (422, "result_key_mismatch")
    assert bucket.heads == []


def test_a_stale_token_is_409_without_asking_s3(leased, bucket):
    w, lease = leased
    assert complete(w, lease, lease_token="stale").status_code == 409
    assert bucket.heads == []


def test_a_repeated_complete_is_accepted_without_a_second_head(leased, bucket):
    w, lease = leased
    upload(w, lease)
    bucket.store(JOB)
    assert complete(w, lease).status_code == 204
    assert complete(w, lease).status_code == 204  # retry after a lost response
    assert len(bucket.heads) == 2  # one at /upload-url, one at the first /complete


def test_s3_trouble_on_complete_is_503_with_retry_after_and_changes_nothing(
    leased, bucket, open_store
):
    w, lease = leased
    upload(w, lease)
    bucket.unavailable = True
    r = complete(w, lease)
    assert r.status_code == 503
    assert r.headers["retry-after"]
    assert open_store().get("j0")["state"] == "leased"


def test_a_second_upload_url_replaces_the_declaration(leased, bucket, open_store):
    w, lease = leased
    upload(w, lease, sha256="d" * 64, size=3)
    upload(w, lease)
    row = open_store().get("j0")
    assert (row["result_sha256"], row["result_size"]) == (SHA, SIZE)


# --- local mode is unchanged ---


def test_local_mode_complete_does_not_look_at_s3(db_url, bucket):
    enqueue(client(db_url, None, ADMIN))
    w = client(db_url, None)
    lease = w.post("/lease", json=CAPS).json()
    assert complete(w, lease).status_code == 204
