"""`fleet-worker`: run a worker configured entirely by environment variables.

  FLEET_BROKER_URL    broker base URL (https, or http to loopback / with FLEET_ALLOW_HTTP=1)
  FLEET_WORKER_TOKEN  bearer token (from the vault or the platform's secret store)
  FLEET_RESULTS_DIR   where results are written (default: results)
  FLEET_RUNNER        which runner to use (default: echo, a demo that needs no GPU)
  FLEET_ALLOW_HTTP    1 to allow plain http to a non-loopback host, e.g. the broker's
                      service name on a private Docker network. Never on a public network.

The same image runs locally, in Compose, on Cloud Run or on Kubernetes: only these change.
"""

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping

import httpx

from .dir_store import DirStore
from .http_broker import HttpBroker
from .loop import Runner, Worker
from .protocol import Job

log = logging.getLogger("fleet_worker")


def echo(job: Job, keepalive: Callable[[], None]) -> bytes:
    """Demo runner: no model, no GPU. Proves the plumbing end to end."""
    return f"echo {job.model} {job.prompt_version} {job.input_sha256}\n".encode()


RUNNERS: dict[str, Runner] = {"echo": echo}


class Config:
    def __init__(self, env: Mapping[str, str]) -> None:
        missing = [n for n in ("FLEET_BROKER_URL", "FLEET_WORKER_TOKEN") if not env.get(n)]
        if missing:
            raise SystemExit(f"fleet-worker: missing {', '.join(missing)}")
        runner = env.get("FLEET_RUNNER", "echo")
        if runner not in RUNNERS:
            raise SystemExit(
                f"fleet-worker: unknown FLEET_RUNNER {runner!r}; have {sorted(RUNNERS)}"
            )
        self.broker_url = env["FLEET_BROKER_URL"]
        self.token = env["FLEET_WORKER_TOKEN"]
        self.results_dir = env.get("FLEET_RESULTS_DIR", "results")
        self.runner = RUNNERS[runner]
        self.allow_http = env.get("FLEET_ALLOW_HTTP") == "1"

    def __repr__(self) -> str:  # never print the token, even by accident
        return f"Config(broker_url={self.broker_url!r}, results_dir={self.results_dir!r})"


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


def build(config: Config) -> tuple[Worker, httpx.Client]:
    client = httpx.Client(
        base_url=config.broker_url,
        headers={"Authorization": f"Bearer {config.token}"},
        timeout=10.0,
    )
    broker = HttpBroker(client, allow_http=config.allow_http)
    return Worker(broker, DirStore(config.results_dir), config.runner), client


def main(env: Mapping[str, str] = os.environ) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config(env)
    worker, client = build(config)
    stopper = Stopper()
    stopper.install()
    log.info("worker started: %r", config)
    with client:
        worker.run(should_stop=stopper.should_stop, sleep=stopper.sleep)
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
