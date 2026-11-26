"""Presigned-URL transfers (ADR 0025): the store that downloads and verifies a job's input and
uploads its result, the broker client's /upload-url call, and the loop around them. No network:
S3 and the broker are httpx MockTransports."""

import hashlib

import httpx
import pytest
from fakes import FakeClock, FakeStore, declares
from fleet_worker.http_broker import HttpBroker
from fleet_worker.loop import (
    InputMismatch,
    InputMissing,
    LeaseLost,
    ResultNotAccepted,
    ResultRejected,
    TransientError,
    UploadRefused,
    Worker,
)
from fleet_worker.protocol import Job, Lease, UploadTarget
from fleet_worker.url_store import UrlStore

INPUT = b"the footage transcript"
CONTEXT = b"the story so far"
SHA = hashlib.sha256(INPUT).hexdigest()
CTX_SHA = hashlib.sha256(CONTEXT).hexdigest()
IN_URL = "https://bucket.s3.example/inputs/in?sig=1"
CTX_URL = "https://bucket.s3.example/inputs/ctx?sig=2"
PUT_URL = "https://bucket.s3.example/results/key?sig=3"
JOB = Job("j0", SHA, "m", "v1")
JOB_CTX = Job("j0", SHA, "m", "v1", CTX_SHA)


def lease(job: Job = JOB, **urls) -> Lease:
    urls = {"input_url": IN_URL} | urls
    return Lease(job, "tok", 60.0, 0.0, **urls)


class FakeS3:
    """An S3 behind a MockTransport: serves `objects` on GET, records and answers PUTs."""

    def __init__(self, objects=None, put_status=200):
        self.objects = {IN_URL: INPUT, CTX_URL: CONTEXT} | (objects or {})
        self.put_status = put_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "PUT":
            return httpx.Response(self.put_status)
        if url in self.objects:
            return httpx.Response(200, content=self.objects[url])
        return httpx.Response(403)  # S3 says 403 for a key that is not there (no ListBucket)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


class FakeUploadBroker:
    def __init__(self, headers=None):
        self.calls: list[tuple] = []
        self.headers = {"x-amz-checksum-sha256": "XYZ", "If-None-Match": "*"} | (headers or {})

    def upload_url(self, lease: Lease, result_key: str, sha256: str, size: int) -> UploadTarget:
        self.calls.append((lease, result_key, sha256, size))
        return UploadTarget(PUT_URL, self.headers)


def store(s3: FakeS3, broker=None, lse: Lease | None = None, **kw) -> UrlStore:
    return UrlStore(broker or FakeUploadBroker(), lse or lease(), s3.client(), **kw)


# --- download and verify ---


def test_the_input_is_downloaded_from_its_url_and_checked():
    s3 = FakeS3()
    store(s3).fetch_inputs()
    assert [str(r.url) for r in s3.requests] == [IN_URL]


def test_the_context_is_downloaded_and_checked_too():
    s3 = FakeS3()
    store(s3, lse=lease(JOB_CTX, context_url=CTX_URL)).fetch_inputs()
    assert [str(r.url) for r in s3.requests] == [IN_URL, CTX_URL]


def test_input_bytes_that_do_not_hash_to_input_sha256_raise():
    s3 = FakeS3({IN_URL: b"tampered"})
    with pytest.raises(InputMismatch, match="input"):
        store(s3).fetch_inputs()


def test_context_bytes_that_do_not_hash_to_context_sha256_raise():
    s3 = FakeS3({CTX_URL: b"another story"})
    with pytest.raises(InputMismatch, match="context"):
        store(s3, lse=lease(JOB_CTX, context_url=CTX_URL)).fetch_inputs()


def test_a_context_hash_without_a_context_url_is_refused():
    # the job names a context but the broker sent no way to get it: running without it would
    # produce a result for the wrong inputs
    with pytest.raises(InputMissing, match="context"):
        store(FakeS3(), lse=lease(JOB_CTX)).fetch_inputs()


def test_a_missing_input_is_input_missing_not_a_crash():
    with pytest.raises(InputMissing):
        store(FakeS3(), lse=lease(input_url="https://bucket.s3.example/inputs/none")).fetch_inputs()


def test_s3_trouble_while_downloading_is_transient():
    class Down(FakeS3):
        def __call__(self, request):
            return httpx.Response(503)

    with pytest.raises(TransientError):
        store(Down()).fetch_inputs()


def test_no_authorization_header_ever_reaches_s3():
    s3 = FakeS3()
    st = store(s3, lse=lease(JOB_CTX, context_url=CTX_URL))
    st.fetch_inputs()
    st.put("k" * 64, b"x")
    assert s3.requests and all("authorization" not in r.headers for r in s3.requests)


def test_plain_http_urls_are_refused_unless_allowed():
    with pytest.raises(ValueError, match="https"):
        store(FakeS3(), lse=lease(input_url="http://bucket.s3.example/inputs/in")).fetch_inputs()


def test_plain_http_to_loopback_is_allowed():
    s3 = FakeS3({"http://127.0.0.1:9000/inputs/in": INPUT})
    store(s3, lse=lease(input_url="http://127.0.0.1:9000/inputs/in")).fetch_inputs()


# --- upload ---


def test_exists_is_always_false_there_is_no_url_to_ask():
    assert store(FakeS3()).exists("k" * 64) is False


def test_put_asks_the_broker_for_a_url_for_this_hash_and_length_then_puts_with_its_headers():
    s3, broker = FakeS3(), FakeUploadBroker()
    data = b"a description"
    store(s3, broker).put("k" * 64, data)
    assert broker.calls == [(lease(), "k" * 64, hashlib.sha256(data).hexdigest(), len(data))]
    (put,) = s3.requests
    assert (put.method, str(put.url)) == ("PUT", PUT_URL)
    assert put.content == data
    assert put.headers["x-amz-checksum-sha256"] == "XYZ"
    assert put.headers["if-none-match"] == "*"
    assert put.headers["content-length"] == str(len(data))


def test_put_treats_precondition_failed_as_already_stored():
    # If-None-Match: * and the object exists: an earlier attempt of this job stored it
    store(FakeS3(put_status=412)).put("k" * 64, b"x")


def test_put_refused_by_s3_is_upload_refused():
    with pytest.raises(UploadRefused):
        store(FakeS3(put_status=403)).put("k" * 64, b"x")
    with pytest.raises(UploadRefused):
        store(FakeS3(put_status=400)).put("k" * 64, b"x")


@pytest.mark.parametrize("status", [409, 429, 500, 503])
def test_put_trouble_at_s3_is_transient(status):
    with pytest.raises(TransientError):
        store(FakeS3(put_status=status)).put("k" * 64, b"x")


# --- the broker client ---


def broker(handler) -> HttpBroker:
    client = httpx.Client(base_url="http://broker", transport=httpx.MockTransport(handler))
    return HttpBroker(client, {"v1"}, {"m"}, clock=FakeClock(), allow_http=True)


LEASE_BODY = {
    "job_id": "j0",
    "input_sha256": SHA,
    "model": "m",
    "prompt_version": "v1",
    "context_sha256": None,
    "lease_token": "t",
    "ttl_seconds": 60,
    "attempts": 1,
}


def test_the_lease_carries_the_urls_when_the_broker_sends_them():
    body = LEASE_BODY | {"input_url": IN_URL, "context_url": None}
    got = broker(lambda r: httpx.Response(200, json=body)).lease()
    assert (got.input_url, got.context_url) == (IN_URL, None)


def test_a_lease_without_urls_has_none():
    got = broker(lambda r: httpx.Response(200, json=LEASE_BODY)).lease()
    assert (got.input_url, got.context_url) == (None, None)


def test_upload_url_posts_the_lease_the_key_the_hash_and_the_size():
    seen = []

    def handler(r):
        seen.append((r.url.path, r.read()))
        return httpx.Response(200, json={"url": PUT_URL, "headers": {"h": "v"}, "expires_in": 300})

    target = broker(handler).upload_url(lease(), "k" * 64, "d" * 64, 12)
    assert target == UploadTarget(PUT_URL, {"h": "v"})
    import json

    assert seen[0][0] == "/upload-url"
    assert json.loads(seen[0][1]) == {
        "job_id": "j0",
        "lease_token": "tok",
        "result_key": "k" * 64,
        "sha256": "d" * 64,
        "size": 12,
    }


def test_upload_url_409_is_lease_lost():
    with pytest.raises(LeaseLost):
        broker(lambda r: httpx.Response(409)).upload_url(lease(), "k" * 64, "d" * 64, 1)


def test_upload_url_key_mismatch_is_result_rejected():
    coded = httpx.Response(422, json={"detail": "x", "code": "result_key_mismatch"})
    with pytest.raises(ResultRejected):
        broker(lambda r: coded).upload_url(lease(), "k" * 64, "d" * 64, 1)


def test_upload_url_413_is_an_invalid_result_not_a_crash():
    from fleet_worker.loop import InvalidResult

    with pytest.raises(InvalidResult, match="too large"):
        broker(lambda r: httpx.Response(413, json={"code": "result_too_large"})).upload_url(
            lease(), "k" * 64, "d" * 64, 1
        )


def test_upload_url_404_means_the_broker_has_no_s3_and_stops_the_worker_loudly():
    with pytest.raises(httpx.HTTPStatusError):
        broker(lambda r: httpx.Response(404, json={"code": "presigned_urls_disabled"})).upload_url(
            lease(), "k" * 64, "d" * 64, 1
        )


@pytest.mark.parametrize("code", ["result_missing", "result_mismatch"])
def test_complete_refused_for_the_stored_result_is_result_not_accepted(code):
    refused = httpx.Response(422, json={"detail": "x", "code": code})
    with pytest.raises(ResultNotAccepted) as e:
        broker(lambda r: refused).complete(lease(), "k" * 64)
    assert code in str(e.value)
    assert not isinstance(e.value, (LeaseLost, TransientError, SystemExit))


# --- the loop ---


def runner_that_records(calls):
    def run(job, keepalive):
        calls.append(job)
        return b"description"

    return declares(run, "v1", models=("m",))


class LoopBroker:
    """The loop's Broker over one lease; complete() records."""

    def __init__(self, lse: Lease):
        self.leases, self.completed = [lse], []

    def lease(self):
        return self.leases.pop(0) if self.leases else None

    def heartbeat(self, lse):
        return lse

    def complete(self, lse, key):
        self.completed.append(key)


def worker(
    lse: Lease, s3: FakeS3, calls, upload_broker=None
) -> tuple[Worker, LoopBroker, FakeStore]:
    plain = FakeStore()
    ub = upload_broker or FakeUploadBroker()
    b = LoopBroker(lse)
    w = Worker(
        b,
        plain,
        runner_that_records(calls),
        url_store=lambda ls: UrlStore(ub, ls, s3.client()),
    )
    return w, b, plain


def test_a_lease_with_urls_downloads_runs_uploads_then_completes():
    calls, s3 = [], FakeS3()
    w, b, plain = worker(lease(), s3, calls)
    assert w.run_once() == "ran"
    assert [r.method for r in s3.requests] == ["GET", "PUT"]
    assert calls == [JOB]
    assert b.completed == [JOB.result_key]
    assert plain.puts == 0  # the local store is not used when the lease carries URLs


def test_an_input_that_fails_verification_is_never_run_stored_or_completed():
    calls, s3 = [], FakeS3({IN_URL: b"tampered"})
    w, b, _ = worker(lease(), s3, calls)
    with pytest.raises(InputMismatch):
        w.run_once()
    assert calls == [] and b.completed == []
    assert [r.method for r in s3.requests] == ["GET"]  # nothing uploaded


def test_the_run_loop_survives_a_bad_input_and_takes_the_next_job():
    calls, s3 = [], FakeS3({IN_URL: b"tampered"})
    w, b, _ = worker(lease(), s3, calls)
    sleeps = []
    stops = iter([False, False, True])
    w.run(should_stop=lambda: next(stops), sleep=sleeps.append)
    assert len(sleeps) == 2  # one backoff after the bad input, one after the empty queue
    assert calls == [] and b.completed == []


def test_the_run_loop_survives_a_refused_stored_result():
    class Refusing(LoopBroker):
        def complete(self, lse, key):
            raise ResultNotAccepted(lse.job.job_id, "result_mismatch")

    calls, s3 = [], FakeS3()
    w, _, _ = worker(lease(), s3, calls)
    w.broker = Refusing(lease())
    stops = iter([False, True])
    w.run(should_stop=lambda: next(stops), sleep=lambda s: None)  # does not raise


def test_a_lease_with_urls_and_no_url_store_is_an_error_not_a_silent_local_run():
    calls = []
    plain = FakeStore()
    w = Worker(LoopBroker(lease()), plain, runner_that_records(calls))
    with pytest.raises(RuntimeError, match="URL"):
        w.run_once()
    assert calls == []


def test_a_lease_without_urls_uses_the_local_store_exactly_as_before():
    calls, s3 = [], FakeS3()
    w, _, plain = worker(Lease(JOB, "tok", 60.0, 0.0), s3, calls)
    assert w.run_once() == "ran"
    assert plain.data == {JOB.result_key: b"description"}
    assert s3.requests == []
