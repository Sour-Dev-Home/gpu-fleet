"""The worker loop: lease a job, keep the lease alive while working, store the result, complete.

The loop only talks to small interfaces (Broker, ResultStore, a clock), so tests use in-memory
fakes, phase 1 plugs in the HTTP broker client and phase 3 an S3 store behind presigned URLs.
"""

import logging
import time
from collections.abc import Callable
from typing import Literal, Protocol

from .backoff import Backoff
from .protocol import Job, Lease

log = logging.getLogger(__name__)


class LeaseLost(Exception):
    """The broker refused a heartbeat: the job may now belong to another worker. Stop, don't complete."""


class TransientError(Exception):
    """The broker is unreachable or failing (network error, 5xx). Wait and try again."""


class InvalidResult(Exception):
    """The runner's output failed validation. It is never stored, so it never gets a content
    address and can never be served from the cache. The lease is left to expire: the job is
    retried and the failure counts toward the dead-letter limit (ADR 0006)."""


def require_nonempty(job: Job, data: bytes) -> None:
    """The default validator. Real runners pass a stricter one (schema, IDs, lengths)."""
    if not isinstance(data, bytes) or not data.strip():
        raise InvalidResult(f"{job.job_id}: empty or non-bytes output")


class Broker(Protocol):
    def lease(self) -> Lease | None:
        """Lease the next job, or None when there is no work."""

    def heartbeat(self, lease: Lease) -> Lease:
        """Renew the lease and return the renewed one (new received_at); raise LeaseLost if refused."""

    def complete(self, lease: Lease, result_key: str) -> None:
        """Report the job done; its result is stored under result_key."""


class ResultStore(Protocol):
    def exists(self, key: str) -> bool: ...

    def put(self, key: str, data: bytes) -> None: ...


# A runner does the actual work (a model call, a transcription). It calls keepalive()
# between chunks of work so the worker can heartbeat; it returns the result bytes.
Runner = Callable[[Job, Callable[[], None]], bytes]

Outcome = Literal["ran", "cached"]


class Worker:
    def __init__(
        self,
        broker: Broker,
        store: ResultStore,
        runner: Runner,
        clock: Callable[[], float] = time.monotonic,
        validate: Callable[[Job, bytes], None] = require_nonempty,
    ) -> None:
        self.broker = broker
        self.store = store
        self.runner = runner
        self.clock = clock
        self.validate = validate
        # the current lease; keepalive() replaces it on renewal
        self.lease: Lease | None = None

    def keepalive(self) -> None:
        """Heartbeat if the current lease is due for renewal. Raises LeaseLost if the broker refuses."""
        assert self.lease is not None
        if self.lease.needs_renewal(self.clock()):
            self.lease = self.broker.heartbeat(self.lease)

    def run_once(self) -> Outcome | None:
        """Lease and process one job. Returns None when the broker has no work."""
        self.lease = self.broker.lease()
        if self.lease is None:
            return None
        try:
            return self.process()
        finally:
            self.lease = None

    def process(self) -> Outcome:
        """Process the job in self.lease: reuse a stored result, or run, store and complete it.

        Returns "cached" if the result already existed, "ran" if the runner produced it.
        """
        assert self.lease is not None
        key = self.lease.job.result_key
        if self.store.exists(key):
            self.broker.complete(self.lease, key)
            return "cached"
        result = self.runner(self.lease.job, self.keepalive)
        self.validate(self.lease.job, result)  # raises InvalidResult: nothing stored or completed
        self.store.put(key, result)
        self.broker.complete(self.lease, key)
        return "ran"

    def run(
        self,
        should_stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
        backoff: Backoff | None = None,
    ) -> None:
        """Process jobs until should_stop() is true. Back off with jitter while the queue is
        empty or the broker is failing (ADR 0008); ask again at once after real work.

        Anything else (a 401, a bug) propagates: a misconfigured worker should stop loudly,
        not retry forever.
        """
        backoff = backoff or Backoff()
        while not should_stop():
            try:
                outcome = self.run_once()
            except LeaseLost as e:
                log.warning("lease lost on %s; moving on", e)
                backoff.reset()
                continue
            except InvalidResult as e:
                log.warning("invalid result, not stored: %s", e)
                sleep(backoff.next_delay())  # a runner that always fails must not spin
                continue
            except TransientError as e:
                log.warning("broker unavailable: %s", e)
                sleep(backoff.next_delay())
                continue
            if outcome is None:
                sleep(backoff.next_delay())
            else:
                backoff.reset()
