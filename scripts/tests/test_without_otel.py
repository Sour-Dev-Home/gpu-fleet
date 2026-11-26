"""The packages work without the [otel] extra (ADR 0018): a volunteer's `pip install fleet-worker`.

Every package test environment includes the extra, so this one runs in a throwaway environment
without it (`uv run --isolated --no-dev`), which leaves the projects' own .venv untouched.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

WORKER_JOB = """
import importlib.util, logging
assert importlib.util.find_spec("opentelemetry") is None, "this check needs an env without OTel"
from fleet_worker import jsonlog, telemetry
from fleet_worker.loop import Worker
from fleet_worker.protocol import Job, Lease

jsonlog.setup()
assert telemetry.setup({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"})() is None
job = Job(job_id="j1", input_sha256="a" * 64, model="m", prompt_version="v1")
class Broker:
    leases = [Lease(job=job, lease_token="t", ttl_seconds=60, received_at=0.0)]
    def lease(self): return self.leases.pop() if self.leases else None
    def heartbeat(self, lease): return lease
    def complete(self, lease, key): self.done = key
class Store:
    data = {}
    def exists(self, k): return k in self.data
    def put(self, k, v): self.data[k] = v
def runner(job, keepalive): return b"text"
runner.prompt_versions, runner.models = frozenset({"v1"}), frozenset({"m"})
broker = Broker()
assert Worker(broker, Store(), runner, lambda: 0.0).run_once() == "ran"
assert broker.done == job.result_key
print("ok")
"""

BROKER_START = """
import importlib.util
# FastAPI itself depends on opentelemetry-api (a no-op on its own); the SDK is what the extra adds.
assert importlib.util.find_spec("opentelemetry.sdk") is None, "this check needs an env without the SDK"
from fleet_broker import telemetry
from fleet_broker.app import create_app
env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}
from fleet_broker.store import JobStore
from psycopg_pool import ConnectionPool
# a closed pool, never connected: without the SDK the gauges (the only reader) are never built
store = JobStore(ConnectionPool("dbname=x", min_size=0, max_size=1, open=False), migrate=False)
assert type(telemetry.setup(store, lambda: 0.0, env=env)) is telemetry.Metrics
assert telemetry.export_logs(env)() is None
print("ok")
"""


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
@pytest.mark.parametrize(
    ("package", "code"),
    [("worker", WORKER_JOB), ("broker", BROKER_START)],
    ids=["worker", "broker"],
)
def test_runs_without_the_otel_extra(package, code, tmp_path):
    out = subprocess.run(
        [
            "uv",
            "run",
            "-q",
            "--isolated",
            "--no-dev",
            "--project",
            str(ROOT / package),
            "python",
            "-c",
            code,
        ],
        check=False,  # the assertion below reports stderr instead
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("ok")
