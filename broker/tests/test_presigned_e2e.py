"""The real worker loop and UrlStore against the real broker app in S3 mode (ADR 0025), with a fake
S3 between them: GET serves the input, PUT stores the result and /complete's HEAD sees it."""

import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient
from fleet_broker import s3
from fleet_broker.app import create_app
from fleet_worker.http_broker import HttpBroker
from fleet_worker.loop import InputMismatch, Worker
from fleet_worker.protocol import result_key
from fleet_worker.url_store import UrlStore
from test_app import ALL, Clock
from test_presigned import FakeBucket
from tokens import ADMIN, WORKER

INPUT = b"transcript of a scene"
SHA = hashlib.sha256(INPUT).hexdigest()


class FakeS3(FakeBucket):
    """The broker's bucket and the worker's HTTPS endpoint in one: what a PUT stores, a HEAD sees."""

    def __init__(self, inputs: dict[str, bytes]) -> None:
        super().__init__()
        self.inputs = inputs
        self.blobs: dict[str, bytes] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.removeprefix("/get/").removeprefix("/put/")
        if request.method == "PUT":
            data = request.content
            sent = request.headers["x-amz-checksum-sha256"]
            if s3.b64_digest(hashlib.sha256(data).hexdigest()) != sent:
                return httpx.Response(400)  # what S3 does when the bytes are not the signed ones
            self.blobs[key] = data
            self.objects[key] = s3.ObjectInfo(len(data), sent)
            return httpx.Response(200)
        return (
            httpx.Response(200, content=self.inputs[key])
            if key in self.inputs
            else httpx.Response(403)
        )


def run(db_url, fake: FakeS3, runner_output: bytes = b"echo result"):
    app = create_app(db_url, WORKER, ADMIN, ALL, ttl_seconds=60, clock=Clock(), bucket=fake)
    admin = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
    job = {
        "job_id": "j0",
        "input_sha256": SHA,
        "model": "m",
        "prompt_version": "v1",
        "data_class": "open",
    }
    assert admin.post("/jobs", json=job).status_code == 201
    api = TestClient(app, headers={"Authorization": f"Bearer {WORKER}"})
    calls = []

    def runner(j, keepalive):
        calls.append(j)
        return runner_output

    runner.prompt_versions, runner.models = frozenset({"v1"}), frozenset({"m"})
    broker = HttpBroker(api, runner.prompt_versions, runner.models, allow_http=True)
    s3_http = httpx.Client(transport=httpx.MockTransport(fake.handle))
    worker = Worker(
        broker,
        None,
        runner,
        url_store=lambda lease: UrlStore(broker, lease, s3_http, allow_http=True),
    )
    return worker, calls


def test_a_job_goes_from_enqueue_to_done_through_s3(db_url, open_store):
    fake = FakeS3({f"inputs/{SHA}": INPUT})
    worker, calls = run(db_url, fake)
    assert worker.run_once() == "ran"
    key = result_key(SHA, "m", "v1")
    assert fake.blobs[f"results/{key}"] == b"echo result"
    assert open_store().get("j0")["state"] == "done"
    assert len(calls) == 1


def test_an_input_the_bucket_corrupted_is_not_run_and_the_job_stays_leased(db_url, open_store):
    fake = FakeS3({f"inputs/{SHA}": b"bit rot"})
    worker, calls = run(db_url, fake)
    with pytest.raises(InputMismatch):
        worker.run_once()
    assert calls == [] and fake.blobs == {}
    assert open_store().get("j0")["state"] == "leased"
