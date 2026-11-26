"""`fleet-worker`: run a worker configured entirely by environment variables.

  FLEET_BROKER_URL    broker base URL (https, or http to loopback / with FLEET_ALLOW_HTTP=1)
  FLEET_WORKER_TOKEN  bearer token (from the vault or the platform's secret store)
  FLEET_RESULTS_DIR   where results are written (default: results)
  FLEET_RUNNER        which runner to use: echo (default, no GPU) or smoke (asks the model server)
  FLEET_MODEL_URL     model server base URL, for runners that use one (smoke)
  FLEET_MODEL         the model the startup check expects on the model server (default: qwen3:8b);
                      each job names the model it is run with
  Each runner implements fixed prompt versions (echo: echo-v1, smoke: smoke-v1); a job with
  another prompt_version stops the worker (misconfiguration, ADR 0014 amendment).
  FLEET_RESULTS_HOST  the results bucket's host, e.g. <bucket>.s3.amazonaws.com (us-east-1) or
                      <bucket>.s3.<region>.amazonaws.com. Required for S3 mode: the worker refuses a
                      presigned URL for any other host (SSRF), and FLEET_ALLOW_HTTP never loosens it.
  FLEET_WORKER_ID     overrides the install ID (default: a random one kept in the user's config folder,
                      see identity.py); the OTel service.instance.id of traces and metrics
  FLEET_ALLOW_HTTP    1 to allow plain http to a non-loopback host, e.g. the broker's
                      service name on a private Docker network. Never on a public network.

The same image runs locally, in Compose, on Cloud Run or on Kubernetes: only these change.
"""

import logging
import os
import re
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Self

import httpx

from . import identity, jsonlog, telemetry
from .dir_store import DirStore
from .http_broker import HttpBroker
from .loop import Runner, Worker
from .protocol import Job
from .smoke import check_model_server, make_smoke
from .url_store import UrlStore

log = logging.getLogger("fleet_worker")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")  # a bare DNS name: no scheme, port, path


def echo(job: Job, keepalive: Callable[[], None]) -> bytes:
    """Demo runner: no model, no GPU. Proves the plumbing end to end."""
    return f"echo {job.model} {job.prompt_version} {job.input_sha256}\n".encode()


# Its own prompt version, so echo bytes never land under a smoke (or real) Result key.
echo.prompt_versions = frozenset({"echo-v1"})
# echo uses no model; jobs for it name the placeholder model "echo" (ADR 0015).
echo.models = frozenset({"echo"})


def _smoke(config: "Config") -> Runner:
    # Generation can take a while on a busy GPU; the heartbeat thread covers the lease.
    client = httpx.Client(base_url=config.model_url, timeout=httpx.Timeout(10.0, read=300.0))
    names = check_model_server(client, config.model)  # the models this worker can run
    return make_smoke(client, names)


# Each entry builds its runner from the config, so runners that need a model server get one.
RUNNERS: dict[str, Callable[["Config"], Runner]] = {
    "echo": lambda config: echo,
    "smoke": _smoke,
}
NEEDS_MODEL_SERVER = {"smoke"}


class Config:
    def __init__(self, env: Mapping[str, str]) -> None:
        runner = env.get("FLEET_RUNNER", "echo")
        required = ["FLEET_BROKER_URL", "FLEET_WORKER_TOKEN"]
        if runner in NEEDS_MODEL_SERVER:
            required.append("FLEET_MODEL_URL")
        missing = [n for n in required if not env.get(n, "").strip()]
        if missing:
            raise SystemExit(f"fleet-worker: missing {', '.join(missing)}")
        if runner not in RUNNERS:
            raise SystemExit(
                f"fleet-worker: unknown FLEET_RUNNER {runner!r}; have {sorted(RUNNERS)}"
            )
        self.broker_url = env["FLEET_BROKER_URL"]
        self.token = env["FLEET_WORKER_TOKEN"].strip()  # "token\n" from a secret store
        self.results_dir = env.get("FLEET_RESULTS_DIR", "results")
        self.runner_name = runner
        self.model_url = env.get("FLEET_MODEL_URL", "")
        self.model = env.get("FLEET_MODEL", "qwen3:8b")
        self.allow_http = env.get("FLEET_ALLOW_HTTP") == "1"
        # The only host presigned URLs may point at (url_store.check_url). Empty: none may.
        self.results_host = env.get("FLEET_RESULTS_HOST", "").strip().lower()
        if self.results_host and not _HOST.match(self.results_host):
            raise SystemExit("fleet-worker: FLEET_RESULTS_HOST must be a bare host name")

    @property
    def results_mode(self) -> str:
        """`local`, or `s3 <host>` when FLEET_RESULTS_HOST is set (the bucket's bare host name)."""
        return f"s3 {self.results_host}" if self.results_host else "local"

    def __repr__(self) -> str:  # never print the token, even by accident
        return (
            f"Config(broker_url={self.broker_url!r}, runner={self.runner_name!r}, "
            f"model_url={self.model_url!r}, results_dir={self.results_dir!r})"
        )


class Stopper:
    """Turns SIGTERM / SIGINT into a clean stop.

    docker stop, Cloud Run scale-in and Kubernetes evictions send SIGTERM, then SIGKILL
    after a grace period (10 s in Docker). The worker finishes the job in hand, takes no
    new lease, and exits. Backoff sleeps wait on an Event, so a stop wakes them at once
    instead of after up to 60 s. A SIGKILL mid-job is still safe: the lease just expires.
    """

    def __init__(self) -> None:
        self.event = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.handle)

    def handle(self, signum: int, frame: object = None) -> None:
        log.info("received signal %s: stopping after the current job", signum)
        self.event.set()

    def should_stop(self) -> bool:
        return self.event.is_set()

    def sleep(self, seconds: float) -> None:
        self.event.wait(seconds)


class _Clients:
    """Closes the broker client and the transfer client together (`with` or close())."""

    def __init__(self, *clients: httpx.Client) -> None:
        self.clients = clients

    def close(self) -> None:
        for c in self.clients:
            c.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def build(config: Config, metrics: telemetry.Metrics | None = None) -> tuple[Worker, _Clients]:
    client = httpx.Client(
        base_url=config.broker_url,
        headers={"Authorization": f"Bearer {config.token}"},
        timeout=10.0,
    )
    runner = RUNNERS[config.runner_name](config)
    broker = HttpBroker(client, runner.prompt_versions, runner.models, allow_http=config.allow_http)
    # For S3 transfers by presigned URL (ADR 0025), used only for leases that carry URLs. Its own
    # client: no base URL and, above all, no Authorization header, because the URLs point at S3.
    # Generation can be slow but a transfer is not: a long read timeout covers a big input.
    transfers = httpx.Client(timeout=httpx.Timeout(10.0, read=300.0))
    worker = Worker(
        broker,
        DirStore(config.results_dir),
        runner,
        url_store=lambda lease: UrlStore(
            broker, lease, transfers, config.results_host, metrics=metrics
        ),
        metrics=metrics,
    )
    return worker, _Clients(client, transfers)


def main(env: Mapping[str, str] = os.environ) -> int:
    jsonlog.setup()  # one JSON object per line on stdout (ADR 0018)
    config = Config(env)
    try:
        instance_id = identity.install_id(env)  # one ID per install, for traces and metrics
    except ValueError as e:
        raise SystemExit(f"fleet-worker: {e}") from None
    # Both are no-ops unless the [otel] extra and OTEL_EXPORTER_OTLP_ENDPOINT are there.
    metrics, flush_metrics = telemetry.setup_metrics(env, instance_id)
    worker, client = build(config, metrics)
    stopper = Stopper()
    stopper.install()
    flush_traces = telemetry.setup(env, instance_id)
    # Labels only (ADR 0026, Q85): no token, and no URL with credentials (the broker URL has none
    # by check; a presigned URL never appears here, only the bucket's host).
    log.info(
        "worker started: %r prompt_versions=%s models=%s results=%s",
        config,
        ",".join(sorted(worker.runner.prompt_versions)),
        ",".join(sorted(worker.runner.models)),
        config.results_mode,
    )
    try:
        with client:
            worker.run(should_stop=stopper.should_stop, sleep=stopper.sleep)
    finally:
        flush_traces()  # the last job's spans, before the process exits
        flush_metrics()  # and the last metrics window
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
