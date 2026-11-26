"""The worker loop: lease a job, keep the lease alive while working, store the result, complete.

The loop only talks to small interfaces (Broker, ResultStore, a clock), so tests use in-memory
fakes, phase 1 plugs in the HTTP broker client and phase 3 an S3 store behind presigned URLs.
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Literal, Protocol, TypeVar

from . import telemetry
from .backoff import Backoff
from .protocol import Job, Lease

log = logging.getLogger(__name__)
T = TypeVar("T")


class LeaseLost(Exception):
    """The broker refused a heartbeat: the job may now belong to another worker. Stop, don't complete."""


class TransientError(Exception):
    """The broker is unreachable or failing (network error, 5xx). Wait and try again."""


RETRIES = 3  # after the first try: waits of 1, 2 and 4 seconds, each up to 50% longer


def retry_transient(
    fn: Callable[[], T],
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
    between: Callable[[], None] = lambda: None,
) -> T:
    """Call fn, retrying a TransientError up to RETRIES times with exponential backoff and jitter.

    A blip at S3 or a 503 from the broker should not cost the job its attempt (3 attempts
    dead-letter it). `between` runs after each wait, before the retry: the Worker passes its
    keepalive so the lease is renewed while it waits. Anything but TransientError, and the last
    TransientError, propagate."""
    for attempt in range(RETRIES + 1):
        try:
            return fn()
        except TransientError as e:
            if attempt == RETRIES:
                raise
            delay = 2.0**attempt * (1 + rng() * 0.5)
            log.warning("transient failure, retrying in %.1fs: %s", delay, e)
            sleep(delay)
            between()
    raise AssertionError("unreachable")


class ResultRejected(SystemExit):
    """The broker refused the result key (422, code result_key_mismatch, ADR 0023): it is not
    the job's content address. Not LeaseLost (the lease is fine) and not transient (a retry
    sends the same key). It means this worker's key formula differs from the broker's, so every
    job would fail the same way: the worker stops, like on an unsupported prompt version, and
    the lease expires. The result object it already stored is harmless (nothing points to it)."""

    def __init__(self, job_id: str):
        super().__init__(
            f"the broker refused the result key for job {job_id}: the worker's key "
            "formula differs from the broker's; stopping"
        )
        self.job_id = job_id


class ResultNotAccepted(Exception):
    """/complete answered 422 with code result_missing or result_mismatch (ADR 0025): the broker
    looked at the stored object and it is not there or not what was declared. The lease is fine
    and this worker's key formula is fine, so unlike ResultRejected it does not stop the worker:
    the lease expires, the job is retried and the failures count toward the dead-letter limit."""

    def __init__(self, job_id: str, code: str):
        super().__init__(f"the broker refused the stored result of job {job_id}: {code}")
        self.job_id = job_id
        self.code = code


class InvalidInput(Exception):
    """The job's input cannot be used: it is missing, or its bytes are not the ones the job names.
    Nothing is run or stored. Like InvalidResult, the lease is left to expire (ADR 0025)."""


class InputMismatch(InvalidInput):
    """Downloaded bytes whose SHA-256 differs from the job's input_sha256 or context_sha256."""


class InputMissing(InvalidInput):
    """No object at the presigned URL (S3 answers 403 or 404), or no URL for a named context."""


class UploadRefused(Exception):
    """S3 refused the result upload (expired URL, wrong length or checksum). Not stored, so not
    completed; the lease expires and the job is retried, as for an invalid result."""


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


class UrlResultStore(ResultStore, Protocol):
    """A store bound to one lease whose input arrives by presigned URL (fleet_worker.url_store)."""

    def fetch_inputs(self) -> None:
        """Download the input (and context) and check their hashes; raise InvalidInput if wrong."""


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

# The `outcome` label of fleet.worker.job.duration for a job that did not finish: one value per
# kind of failure the loop already tells apart. Anything else is "error".
_FAILURES = {
    LeaseLost: "lease_lost",
    InvalidResult: "invalid_result",
    InvalidInput: "invalid_input",
    InputMismatch: "invalid_input",
    InputMissing: "invalid_input",
    UploadRefused: "upload_refused",
    ResultNotAccepted: "result_not_accepted",
    TransientError: "transient",
}


class Worker:
    def __init__(
        self,
        broker: Broker,
        store: ResultStore,
        runner: Runner,
        clock: Callable[[], float] = time.monotonic,
        validate: Callable[[Job, bytes], None] = require_nonempty,
        heartbeat_poll_seconds: float = 1.0,
        url_store: Callable[[Lease], UrlResultStore] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        metrics: telemetry.Metrics | None = None,
    ) -> None:
        self.metrics = metrics or telemetry.Metrics()
        if not isinstance(getattr(runner, "prompt_versions", None), frozenset):
            raise TypeError(f"runner {runner!r} must declare prompt_versions (a frozenset)")
        if not isinstance(getattr(runner, "models", None), frozenset):
            raise TypeError(f"runner {runner!r} must declare models (a frozenset)")
        self.broker = broker
        self.store = store
        # Builds the store for a lease that carries presigned URLs (S3 mode, ADR 0025); the
        # `store` above serves every other lease, exactly as before.
        self.url_store = url_store
        # Used to wait between retries of an upload or a completion (retry_transient).
        self._retry_sleep, self._retry_rng = sleep, rng
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

    def _store_for(self, lease: Lease) -> ResultStore:
        if lease.input_url is None:
            return self.store
        if self.url_store is None:
            # Falling back to the local store would skip the input check and write where the
            # broker will never look.
            raise RuntimeError("the broker sent presigned URLs but this worker has no URL store")
        return self.url_store(lease)

    def _run_with_heartbeat(self, job: Job, store: ResultStore) -> bytes:
        """Fetch and check the input if it comes by URL, then run the runner, while a background
        thread keeps the lease alive (a large download can outlast the lease TTL).

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
            fetch = getattr(store, "fetch_inputs", None)
            if fetch is not None:
                fetch()  # raises InvalidInput: the runner never sees unverified bytes
            result = self.runner(job, self.keepalive)
        finally:
            done.set()
            thread.join()
        if problem:
            if isinstance(problem[0], LeaseLost):
                # The work is done and valid bytes are content-addressed: keep them, so whoever
                # holds the job now finds the result and completes without running it again.
                self.validate(job, result)
                store.put(job.result_key, result)
            raise problem[0]
        return result

    def _retry(self, fn: Callable[[], T]) -> T:
        """fn with retry_transient, renewing the lease between attempts (a LeaseLost from that
        renewal ends the retries: the job is no longer ours to finish)."""
        return retry_transient(fn, self._retry_sleep, self._retry_rng, between=self.keepalive)

    def run_once(self) -> Outcome | None:
        """Lease and process one job. Returns None when the broker has no work."""
        asked = time.time_ns()
        self.lease = self.broker.lease()
        if self.lease is None:
            return None  # no trace for an empty poll: an idle fleet would bury the real ones
        job = self.lease.job
        started, outcome = self.clock(), "error"  # error: anything not named below
        try:
            # One trace per attempt, backdated to when the lease was asked for (ADR 0018).
            with telemetry.span(
                "job",
                start_time=asked,
                **{
                    "fleet.job_id": job.job_id,
                    "fleet.model": job.model,
                    "fleet.prompt_version": job.prompt_version,
                },
            ):
                with telemetry.span("lease", start_time=asked):
                    pass  # ends now: the time the broker took to hand the job out
                outcome = self.process()
                return outcome
        except BaseException as e:
            outcome = _FAILURES.get(type(e), "error")
            raise
        finally:
            # Labels only, never the job ID (ADR 0018, 0026): one series per outcome, not per job.
            self.metrics.job(
                max(0.0, self.clock() - started), job.model, job.prompt_version, outcome
            )
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
        store = self._store_for(self.lease)
        # IDs and labels only (jsonlog.ALLOWED_EXTRAS): never the input or the result.
        fields = {"job_id": job.job_id, "model": job.model, "prompt_version": job.prompt_version}
        if store.exists(key):
            with telemetry.span("complete", **{"fleet.cached": True}):
                self._retry(lambda: self.broker.complete(self.lease, key))
            log.info("job completed from a stored result", extra={**fields, "status": "cached"})
            return "cached"
        with telemetry.span("run"):  # the model call: where a slow job spends its time
            result = self._run_with_heartbeat(self.lease.job, store)
        self.validate(self.lease.job, result)  # raises InvalidResult: nothing stored or completed
        with telemetry.span("store"):
            self._retry(lambda: store.put(key, result))
        with telemetry.span(
            "complete"
        ):  # its POST carries traceparent: the broker's span nests here
            self._retry(lambda: self.broker.complete(self.lease, key))
        log.info("job completed", extra={**fields, "status": "ran"})
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
            except ResultRejected as e:
                log.error("broker refused the result key for job %s; stopping", e.job_id)
                raise  # not retried and not skipped: see the class docstring
            except (InvalidResult, InvalidInput, UploadRefused, ResultNotAccepted) as e:
                log.warning("job not completed, lease left to expire: %s", e)
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
