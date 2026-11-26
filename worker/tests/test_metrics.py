"""Worker metrics (Q87, ADR 0026, ADR 0018): job and transfer durations, read back through the SDK."""

import logging

import httpx
import pytest
from fakes import FakeBroker, FakeClock, FakeStore, declares
from fleet_worker import cli, telemetry
from fleet_worker.loop import InvalidResult, LeaseLost, Worker
from fleet_worker.protocol import Job, Lease, UploadTarget
from fleet_worker.url_store import UrlStore
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

JOB = Job(job_id="j1", input_sha256="a" * 64, model="qwen3-vl:8b", prompt_version="describe-v1")
HOST = "bucket.s3.amazonaws.com"


def rig():
    reader = InMemoryMetricReader()
    return telemetry.OtelMetrics(MeterProvider(metric_readers=[reader])), reader


def histograms(reader) -> dict[str, list]:
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = list(m.data.data_points)
    return out


def worker(runner, metrics, clock=None, store=None):
    clock, store = clock or FakeClock(), store or FakeStore()
    leased = [Lease(job=JOB, lease_token="t", ttl_seconds=60, received_at=clock())]
    return Worker(
        FakeBroker(leased, store, clock),
        store,
        declares(runner, JOB.prompt_version),
        clock,
        metrics=metrics,
    )


def test_a_run_job_records_its_duration_by_model_version_and_outcome():
    metrics, reader = rig()
    clock = FakeClock()

    def runner(job, keepalive):
        clock.advance(12.5)
        return b"text"

    worker(runner, metrics, clock).run_once()
    (point,) = histograms(reader)["fleet.worker.job.duration"]
    assert dict(point.attributes) == {
        "model": "qwen3-vl:8b",
        "prompt_version": "describe-v1",
        "outcome": "ran",
    }
    assert (point.count, point.sum) == (1, 12.5)


def test_a_stored_result_is_the_cached_outcome():
    metrics, reader = rig()
    store = FakeStore()
    store.data[JOB.result_key] = b"already"
    worker(lambda job, keepalive: b"never", metrics, store=store).run_once()
    (point,) = histograms(reader)["fleet.worker.job.duration"]
    assert point.attributes["outcome"] == "cached"


@pytest.mark.parametrize(
    ("error", "outcome"),
    [(InvalidResult("empty"), "invalid_result"), (RuntimeError("boom"), "error")],
)
def test_a_failed_job_is_recorded_with_its_outcome_and_still_raises(error, outcome):
    metrics, reader = rig()

    def runner(job, keepalive):
        raise error

    with pytest.raises(type(error)):
        worker(runner, metrics).run_once()
    (point,) = histograms(reader)["fleet.worker.job.duration"]
    assert point.attributes["outcome"] == outcome


def test_a_lost_lease_is_its_own_outcome():
    metrics, reader = rig()

    def runner(job, keepalive):
        raise LeaseLost("j1")

    with pytest.raises(LeaseLost):
        worker(runner, metrics).run_once()
    (point,) = histograms(reader)["fleet.worker.job.duration"]
    assert point.attributes["outcome"] == "lease_lost"


def test_an_empty_poll_records_nothing():
    metrics, reader = rig()
    w = Worker(
        FakeBroker([], FakeStore(), FakeClock()), FakeStore(), declares(lambda j, k: b"x", "v")
    )
    w.metrics = metrics
    assert w.run_once() is None
    assert "fleet.worker.job.duration" not in histograms(reader)


def test_no_label_carries_a_job_id():
    metrics, reader = rig()
    worker(lambda job, keepalive: b"text", metrics).run_once()
    for points in histograms(reader).values():
        for p in points:
            assert "job_id" not in p.attributes and "fleet.job_id" not in p.attributes
            assert "j1" not in p.attributes.values()


# --- transfers ---


class UploadBroker:
    def upload_url(self, lease, result_key, sha256, size):
        return UploadTarget(url=f"https://{HOST}/results/x", headers={})


def url_store(handler, metrics, clock):
    import hashlib

    job = Job(
        job_id="j1",
        input_sha256=hashlib.sha256(b"input").hexdigest(),
        model="m",
        prompt_version="v",
    )
    lease = Lease(
        job=job, lease_token="t", ttl_seconds=60, received_at=0.0, input_url=f"https://{HOST}/in"
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return UrlStore(UploadBroker(), lease, http, HOST, metrics=metrics, clock=clock)


def test_download_and_upload_are_timed_by_direction():
    metrics, reader = rig()
    clock = FakeClock()

    def handler(request):
        clock.advance(2.0 if request.method == "GET" else 3.0)
        return httpx.Response(200, content=b"input" if request.method == "GET" else b"")

    store = url_store(handler, metrics, clock)
    store.fetch_inputs()
    store.put("k" * 64, b"result")
    points = {
        p.attributes["direction"]: (p.count, p.sum)
        for p in histograms(reader)["fleet.worker.transfer.duration"]
    }
    assert points == {"download": (1, 2.0), "upload": (1, 3.0)}


def test_a_failed_transfer_is_not_timed_as_a_transfer():
    metrics, reader = rig()
    store = url_store(lambda request: httpx.Response(403), metrics, FakeClock())
    with pytest.raises(Exception):  # noqa: B017 - InputMissing, whatever its base
        store.fetch_inputs()
    assert "fleet.worker.transfer.duration" not in histograms(reader)


# --- setup ---


def test_the_resource_carries_the_install_id_as_the_instance():
    res = telemetry.resource({"OTEL_SERVICE_NAME": "w"}, "install-1")
    assert res.attributes["service.instance.id"] == "install-1"
    assert res.attributes["service.name"] == "w"
    assert telemetry.resource({}, "i").attributes["service.name"] == "fleet-worker"


def test_metrics_are_off_unless_an_endpoint_is_set():
    metrics, flush = telemetry.setup_metrics({}, "id")
    assert type(metrics) is telemetry.Metrics
    assert flush() is None
    off = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "OTEL_SDK_DISABLED": "true"}
    assert type(telemetry.setup_metrics(off, "id")[0]) is telemetry.Metrics


def test_metrics_export_over_plain_http_is_refused_off_this_machine():
    with pytest.raises(ValueError, match="refusing"):
        telemetry.setup_metrics(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4318"}, "i"
        )


def test_metrics_go_to_the_metrics_path():
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318/"}
    assert telemetry.metrics_endpoint(env) == "http://127.0.0.1:4318/v1/metrics"


# --- the start line and the ID reaching the exporters ---


@pytest.fixture
def started(monkeypatch, tmp_path):
    """cli.main with everything that touches the process or the network stubbed out."""
    monkeypatch.setattr(cli.jsonlog, "setup", lambda: None)
    monkeypatch.setattr(cli.Stopper, "install", lambda self: None)
    monkeypatch.setattr(cli.Worker, "run", lambda self, **kw: None)
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        cli.telemetry,
        "setup",
        lambda env, instance_id=None: seen.update(traces=instance_id) or (lambda: None),
    )
    monkeypatch.setattr(
        cli.telemetry,
        "setup_metrics",
        lambda env, instance_id: (
            seen.update(metrics=instance_id) or (telemetry.Metrics(), lambda: None)
        ),
    )
    return seen


ENV = {"FLEET_BROKER_URL": "http://127.0.0.1:8000", "FLEET_WORKER_TOKEN": "t" * 40}


def test_traces_and_metrics_share_the_install_id(started, tmp_path):
    env = {**ENV, "FLEET_WORKER_ID": "install-9"}
    assert cli.main(env) == 0
    assert started == {"traces": "install-9", "metrics": "install-9"}


def test_the_start_line_lists_versions_models_and_local_results(started, caplog):
    caplog.set_level(logging.INFO, logger="fleet_worker")
    cli.main({**ENV, "FLEET_WORKER_ID": "i"})
    (line,) = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("worker started")
    ]
    assert "prompt_versions=echo-v1" in line
    assert "models=echo" in line
    assert "results=local" in line
    assert "127.0.0.1:8000" in line  # the broker URL was already logged


def test_the_start_line_names_the_results_host_in_s3_mode(started, caplog):
    caplog.set_level(logging.INFO, logger="fleet_worker")
    cli.main({**ENV, "FLEET_WORKER_ID": "i", "FLEET_RESULTS_HOST": HOST})
    (line,) = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("worker started")
    ]
    assert f"results=s3 {HOST}" in line


def test_the_start_line_has_no_token_and_no_credentials(started, caplog):
    caplog.set_level(logging.INFO, logger="fleet_worker")
    cli.main({**ENV, "FLEET_WORKER_ID": "i", "FLEET_RESULTS_HOST": HOST})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert ENV["FLEET_WORKER_TOKEN"] not in text
    assert "X-Amz" not in text
    assert "https://" not in text
