"""Broker metrics (ADR 0018), read back through the real SDK with an in-memory reader."""

from fastapi.testclient import TestClient
from fleet_broker import telemetry
from fleet_broker.app import create_app
from fleet_broker.store import JobStore
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from tokens import ADMIN, WORKER

NOW = 1_800_000_000.0
LEASE = {"capabilities": ["v1"], "models": ["m"]}
JOB = {"input_sha256": "a" * 64, "model": "m", "prompt_version": "v1", "data_class": "open"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def rig(tmp_path):
    """A broker app with OTel metrics read by an in-memory reader."""
    clock, reader = Clock(), InMemoryMetricReader()
    db = tmp_path / "broker.db"
    reader_store = JobStore(db)
    metrics = telemetry.OtelMetrics(
        MeterProvider(metric_readers=[reader]), lambda: (clock(), reader_store.stats(clock()))
    )
    app = create_app(
        db, WORKER, ADMIN, {"open"}, ttl_seconds=60, clock=clock, metrics=metrics, busy_timeout=0.05
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
        "result_key": "f" * 64,
    }
    r = client.post("/complete", json=body, headers={"Authorization": f"Bearer {WORKER}"})
    assert r.status_code == 204


def test_queue_gauges_read_the_database(tmp_path):
    client, clock, reader = rig(tmp_path)
    add(client, "a")
    clock.now += 100
    add(client, "b")
    lease(client)  # "a" runs; "b" waits
    clock.now += 20
    p = points(reader)
    jobs = {(d.attributes["state"], d.attributes["data_class"]): d.value for d in p["fleet.jobs"]}
    assert jobs == {("leased", "open"): 1, ("queued", "open"): 1}
    (age,) = p["fleet.queue.oldest_age"]
    assert age.value == 20  # "b" has waited 20 s; the running "a" doesn't count


def test_completion_counts_once_and_records_turnaround(tmp_path):
    client, clock, reader = rig(tmp_path)
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


def test_the_slo_threshold_is_a_bucket_edge():
    # "done within 15 minutes" is exact only if 900 s is an edge (ADR 0018).
    assert 900 in telemetry.TURNAROUND_BUCKETS


def test_a_lease_after_expiry_is_counted(tmp_path):
    client, clock, reader = rig(tmp_path)
    add(client, "a")
    lease(client)
    clock.now += 61  # the worker vanished; its lease ran out
    assert lease(client)["attempts"] == 2
    (reissued,) = points(reader)["fleet.leases.reissued"]
    assert reissued.value == 1


def test_busy_database_responses_are_counted(tmp_path):
    import sqlite3

    client, _, reader = rig(tmp_path)
    holder = sqlite3.connect(tmp_path / "broker.db", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")  # another writer holds the lock
    try:
        r = client.post(
            "/jobs", json={"job_id": "a", **JOB}, headers={"Authorization": f"Bearer {ADMIN}"}
        )
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.status_code == 503
    (busy,) = points(reader)["fleet.http.busy"]
    assert busy.value == 1


def test_broker_shutdown_flushes_metrics(tmp_path):
    class Spy(telemetry.Metrics):
        stopped = False

        def shutdown(self) -> None:
            self.stopped = True

    spy = Spy()
    app = create_app(tmp_path / "broker.db", WORKER, ADMIN, {"open"}, metrics=spy)
    with TestClient(app):  # entering and leaving runs the app's startup and shutdown
        assert not spy.stopped
    assert spy.stopped


def test_off_without_an_endpoint_or_when_disabled(tmp_path):
    db = tmp_path / "broker.db"
    assert type(telemetry.setup(db, Clock(), env={})) is telemetry.Metrics
    disabled = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "OTEL_SDK_DISABLED": "true"}
    assert type(telemetry.setup(db, Clock(), env=disabled)) is telemetry.Metrics


def test_on_with_an_endpoint(tmp_path):
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9/"}
    m = telemetry.setup(tmp_path / "broker.db", Clock(), env=env)
    try:
        assert isinstance(m, telemetry.OtelMetrics)
        assert telemetry.metrics_endpoint(env) == "http://127.0.0.1:9/v1/metrics"
    finally:
        m.provider.shutdown(timeout_millis=1)  # nothing listens on port 9: don't wait on it
