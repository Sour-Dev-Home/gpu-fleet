"""Broker metrics (ADR 0018), read back through the real SDK with an in-memory reader."""

from contextlib import ExitStack

from conftest import make_store
from fastapi.testclient import TestClient
from fleet_broker import telemetry
from fleet_broker.addressing import result_key
from fleet_broker.app import create_app
from fleet_broker.store import JobStore
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from tokens import ADMIN, WORKER

NOW = 1_800_000_000.0
LEASE = {"capabilities": ["v1"], "models": ["m"]}
JOB = {"input_sha256": "a" * 64, "model": "m", "prompt_version": "v1", "data_class": "open"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def rig(db_url):
    """A broker app with OTel metrics read by an in-memory reader."""
    clock, reader = Clock(), InMemoryMetricReader()
    reader_store = make_store(db_url)
    metrics = telemetry.OtelMetrics(
        MeterProvider(metric_readers=[reader]), lambda: (clock(), reader_store.stats(clock()))
    )
    app = create_app(
        db_url,
        WORKER,
        ADMIN,
        {"open"},
        ttl_seconds=60,
        clock=clock,
        metrics=metrics,
        pool_timeout=0.3,
    )
    return TestClient(app), clock, reader


def points(reader) -> dict[str, list]:
    """metric name -> its data points, from one collection."""
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = list(m.data.data_points)
    return out


def add(client, job_id):
    r = client.post(
        "/jobs", json={"job_id": job_id, **JOB}, headers={"Authorization": f"Bearer {ADMIN}"}
    )
    assert r.status_code == 201


def lease(client):
    return client.post("/lease", json=LEASE, headers={"Authorization": f"Bearer {WORKER}"}).json()


def complete(client, leased):
    body = {
        "job_id": leased["job_id"],
        "lease_token": leased["lease_token"],
        "result_key": result_key(
            leased["input_sha256"],
            leased["model"],
            leased["prompt_version"],
            leased.get("context_sha256"),
        ),
    }
    r = client.post("/complete", json=body, headers={"Authorization": f"Bearer {WORKER}"})
    assert r.status_code == 204


def test_queue_gauges_read_the_database(db_url):
    client, clock, reader = rig(db_url)
    add(client, "a")
    clock.now += 100
    add(client, "b")
    lease(client)  # "a" runs; "b" waits
    clock.now += 20
    p = points(reader)
    jobs = {(d.attributes["state"], d.attributes["data_class"]): d.value for d in p["fleet.jobs"]}
    nonzero = {k: v for k, v in jobs.items() if v}
    assert nonzero == {("leased", "open"): 1, ("queued", "open"): 1}
    # Zeros are reported too, so an emptied state reads 0 at once instead of going stale.
    assert len(jobs) == len(telemetry.JOB_STATES) * len(telemetry.DATA_CLASSES)
    (age,) = p["fleet.queue.oldest_age"]
    assert age.value == 20  # "b" has waited 20 s; the running "a" doesn't count


def test_cancelled_jobs_are_counted_and_no_longer_wait(db_url):
    client, clock, reader = rig(db_url)
    add(client, "a")
    r = client.post("/jobs/a/cancel", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200
    clock.now += 50
    p = points(reader)
    jobs = {(d.attributes["state"], d.attributes["data_class"]): d.value for d in p["fleet.jobs"]}
    assert jobs[("cancelled", "open")] == 1
    assert jobs[("queued", "open")] == 0
    (age,) = p["fleet.queue.oldest_age"]
    assert age.value == 0  # a cancelled job is not waiting


def test_completion_counts_once_and_records_turnaround(db_url):
    client, clock, reader = rig(db_url)
    add(client, "a")
    leased = lease(client)
    clock.now += 42
    complete(client, leased)
    complete(client, leased)  # the worker retries after a lost response
    p = points(reader)
    (done,) = p["fleet.jobs.completed"]
    assert done.value == 1
    assert dict(done.attributes) == {"model": "m", "prompt_version": "v1", "data_class": "open"}
    (turnaround,) = p["fleet.job.turnaround"]
    assert turnaround.count == 1 and turnaround.sum == 42


def test_a_refused_result_key_is_counted_and_is_not_a_completion(db_url):
    client, _clock, reader = rig(db_url)
    add(client, "a")
    leased = lease(client)
    body = {"job_id": "a", "lease_token": leased["lease_token"], "result_key": "f" * 64}
    r = client.post("/complete", json=body, headers={"Authorization": f"Bearer {WORKER}"})
    assert r.status_code == 422
    p = points(reader)
    (rejected,) = p["fleet.results.rejected"]
    assert rejected.value == 1
    assert "fleet.jobs.completed" not in p


def test_gauge_labels_cover_every_state_and_data_class():
    from fleet_broker import app, store

    assert set(telemetry.DATA_CLASSES) == set(app.DATA_CLASSES)
    for state in ("queued", "leased", "done", "dead", "cancelled"):  # the CHECK constraint's states
        assert f"'{state}'" in store.SCHEMA and state in telemetry.JOB_STATES


def test_traceparent_is_honoured_for_every_current_worker_token():
    # During a token rotation both worker tokens are valid, so both may join traces.
    from opentelemetry import trace

    propagator = telemetry._authenticated_propagator(("token-new-" * 4, "token-old-" * 4))
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    def joined(auth: str) -> bool:
        carrier = {"traceparent": [parent], "authorization": [auth]}
        ctx = propagator.extract(carrier, getter=_ListGetter())
        return trace.get_current_span(ctx).get_span_context().is_valid

    assert joined("Bearer " + "token-new-" * 4)
    assert joined("Bearer " + "token-old-" * 4)
    assert not joined("Bearer " + "token-xyz-" * 4)


class _ListGetter:
    """The ASGI getter's shape: header name -> list of values."""

    def get(self, carrier, key):
        return carrier.get(key)

    def keys(self, carrier):
        return list(carrier)


def test_the_slo_threshold_is_a_bucket_edge():
    # "done within 15 minutes" is exact only if 900 s is an edge (ADR 0018).
    assert 900 in telemetry.TURNAROUND_BUCKETS


def test_a_lease_after_expiry_is_counted(db_url):
    client, clock, reader = rig(db_url)
    add(client, "a")
    lease(client)
    clock.now += 61  # the worker vanished; its lease ran out
    assert lease(client)["attempts"] == 2
    (reissued,) = points(reader)["fleet.leases.reissued"]
    assert reissued.value == 1


def test_busy_database_responses_are_counted(db_url):
    client, _, reader = rig(db_url)
    with ExitStack() as held:  # every pooled connection is in use: the request times out (0.3 s)
        for _ in range(client.app.state.pool.max_size):
            held.enter_context(client.app.state.pool.connection())
        r = client.post(
            "/jobs", json={"job_id": "a", **JOB}, headers={"Authorization": f"Bearer {ADMIN}"}
        )
    assert r.status_code == 503
    (busy,) = points(reader)["fleet.http.busy"]
    assert busy.value == 1


def test_rate_limited_responses_are_counted(db_url):
    clock, reader = Clock(), InMemoryMetricReader()
    reader_store = make_store(db_url)
    metrics = telemetry.OtelMetrics(
        MeterProvider(metric_readers=[reader]), lambda: (clock(), reader_store.stats(clock()))
    )
    app = create_app(db_url, WORKER, ADMIN, {"open"}, clock=clock, metrics=metrics, worker_burst=1)
    client = TestClient(app)
    assert [lease_status(client) for _ in range(3)] == [204, 429, 429]
    (limited,) = points(reader)["fleet.http.limited"]
    assert limited.value == 2


def lease_status(client) -> int:
    headers = {"Authorization": f"Bearer {WORKER}"}
    return client.post("/lease", json=LEASE, headers=headers).status_code


def test_broker_shutdown_flushes_metrics_and_closes_the_pool(db_url):
    class Spy(telemetry.Metrics):
        stopped = False

        def shutdown(self) -> None:
            self.stopped = True

    spy = Spy()
    app = create_app(db_url, WORKER, ADMIN, {"open"}, metrics=spy)
    with TestClient(app):  # entering and leaving runs the app's startup and shutdown
        assert not spy.stopped
        assert not app.state.pool.closed
    assert spy.stopped
    assert app.state.pool.closed  # SIGTERM or not, the connections end cleanly


def test_off_without_an_endpoint_or_when_disabled(db_url):
    store = make_store(db_url)
    assert type(telemetry.setup(store, Clock(), env={})) is telemetry.Metrics
    disabled = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "OTEL_SDK_DISABLED": "true"}
    assert type(telemetry.setup(store, Clock(), env=disabled)) is telemetry.Metrics


def test_on_with_an_endpoint(db_url):
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9/"}
    m = telemetry.setup(make_store(db_url), Clock(), env=env)
    try:
        assert isinstance(m, telemetry.OtelMetrics)
        assert telemetry.metrics_endpoint(env) == "http://127.0.0.1:9/v1/metrics"
    finally:
        m.provider.shutdown(timeout_millis=1)  # nothing listens on port 9: don't wait on it


class Collect(MetricExporter):
    """Keeps what the reader exports, so a test can read the final export."""

    def __init__(self) -> None:
        super().__init__()
        self.exported = []

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        self.exported.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        pass


def test_the_last_export_on_shutdown_still_reads_the_gauges(db_url):
    # SIGTERM: the final collection calls the gauge callbacks, which query the database through
    # the app's pool. The pool must close AFTER the metrics shut down, or the last window is lost.
    exporter, errors, holder = Collect(), [], {}

    def stats():
        try:
            return NOW, JobStore(holder["app"].state.pool, migrate=False).stats(NOW)
        except Exception as e:
            errors.append(e)
            raise

    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=3_600_000)
    metrics = telemetry.OtelMetrics(MeterProvider(metric_readers=[reader]), stats)
    holder["app"] = create_app(db_url, WORKER, ADMIN, {"open"}, metrics=metrics)
    with TestClient(holder["app"]) as client:
        add(client, "a")
    assert errors == []
    final = [
        point
        for data in exporter.exported
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "fleet.jobs"
        for point in m.data.data_points
    ]
    assert sum(p.value for p in final) == 1  # the queued job, read from the database
