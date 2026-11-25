"""The worker loop: lease a job, keep the lease alive while working, store the result, complete.

The loop only talks to small interfaces (Broker, ResultStore, a clock), so tests use in-memory
fakes, phase 1 plugs in the HTTP broker client and phase 3 an S3 store behind presigned URLs.
"""

import logging
import threading
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


class UnsupportedPromptVersion(SystemExit):
    """A job asks for a prompt version this worker's runner doesn't implement.

    That is misconfiguration (the wrong FLEET_RUNNER, or jobs enqueued for another runner), not
    a bad job: the worker stops with this message instead of burning the job's attempts, and
    stores and completes nothing. The lease simply expires (ADR 0014 amendment).
    """


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


class Runner(Protocol):
    """Does the actual work (a model call, a transcription) and returns the result bytes.

    A plain function with a prompt_versions attribute: the prompt versions it implements.
    The prompt version is part of the Result key, so only the runner that implements a
    version may write results under it. keepalive() is optional (see Worker.keepalive).
    """

    prompt_versions: frozenset[str]
    # The model names it can run; declared to the broker in every lease request (ADR 0015).
    models: frozenset[str]

    def __call__(self, job: Job, keepalive: Callable[[], None], /) -> bytes: ...


Outcome = Literal["ran", "cached"]


class Worker:
    def __init__(
        self,
        broker: Broker,
        store: ResultStore,
        runner: Runner,
        clock: Callable[[], float] = time.monotonic,
        validate: Callable[[Job, bytes], None] = require_nonempty,
        heartbeat_poll_seconds: float = 1.0,
    ) -> None:
        if not isinstance(getattr(runner, "prompt_versions", None), frozenset):
            raise TypeError(f"runner {runner!r} must declare prompt_versions (a frozenset)")
        if not isinstance(getattr(runner, "models", None), frozenset):
            raise TypeError(f"runner {runner!r} must declare models (a frozenset)")
        self.broker = broker
        self.store = store
        self.runner = runner
        self.clock = clock
        self.validate = validate
        # How often the background heartbeat checks whether the lease is due. Renewal happens
        # at 2/3 of the TTL; polling every second is free and far finer than any real TTL.
        self.heartbeat_poll_seconds = heartbeat_poll_seconds
        # the current lease; keepalive() replaces it on renewal
        self.lease: Lease | None = None
        # keepalive() runs on two threads (the runner's and the heartbeat's): one at a time
        self._lease_lock = threading.Lock()

    def keepalive(self) -> None:
        """Heartbeat if the current lease is due for renewal. Raises LeaseLost if the broker refuses.

        Runners may still call it, but don't need to: process() also renews from a background
        thread while the runner works (ADR 0014), so a runner can block for as long as it likes.
        """
        with self._lease_lock:
            assert self.lease is not None
            if self.lease.needs_renewal(self.clock()):
                self.lease = self.broker.heartbeat(self.lease)

    def _run_with_heartbeat(self, job: Job) -> bytes:
        """Run the runner while a background thread keeps the lease alive.

        Raises LeaseLost (after the runner returns) if the broker refused a renewal, and
        re-raises anything fatal the heartbeat hit. A TransientError (broker briefly
        unreachable) is retried at the next poll: the lease usually has a third of its TTL left.
        """
        done = threading.Event()
        problem: list[Exception] = []

        def beat() -> None:
            while not done.wait(self.heartbeat_poll_seconds):
                try:
                    self.keepalive()
                except TransientError as e:
                    log.warning("heartbeat failed, retrying: %s", e)
                # LeaseLost, 401, bugs: stop beating and hand the error to the main thread,
                # which re-raises it. Uncaught, it would die silently with this thread.
                except Exception as e:  # noqa: BLE001
                    problem.append(e)
                    return

        thread = threading.Thread(target=beat, name="heartbeat", daemon=True)
        thread.start()
        try:
            result = self.runner(job, self.keepalive)
        finally:
            done.set()
            thread.join()
        if problem:
            if isinstance(problem[0], LeaseLost):
                # The work is done and valid bytes are content-addressed: keep them, so whoever
                # holds the job now finds the result and completes without running it again.
                self.validate(job, result)
                self.store.put(job.result_key, result)
            raise problem[0]
        return result

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
        job = self.lease.job
        # Before the store: a misrouted job must not even reuse a result someone else wrote.
        if job.prompt_version not in self.runner.prompt_versions:
            name = getattr(self.runner, "__name__", repr(self.runner))
            raise UnsupportedPromptVersion(
                f"fleet-worker: job {job.job_id} has prompt_version {job.prompt_version!r}, but "
                f"runner {name!r} implements {sorted(self.runner.prompt_versions)}. Stopping: "
                "enqueue jobs for this runner or start the worker with the matching FLEET_RUNNER."
            )
        key = job.result_key
        if self.store.exists(key):
            self.broker.complete(self.lease, key)
            return "cached"
        result = self._run_with_heartbeat(self.lease.job)
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
