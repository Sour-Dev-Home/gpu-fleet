"""The broker's HTTP API: lease, heartbeat, complete (workers) and add jobs (admin).

Status codes carry the store's answers (ADR 0007):
- 200 with a body: done.
- 204 from /lease: no work right now (normal; the worker backs off).
- 409 from /heartbeat or /complete: the caller no longer owns this job (stale token).
- 401: missing or wrong bearer token. 413: request body too large. 422: malformed fields.

Two tokens (ADR 0011): workers can only lease, heartbeat and complete; only the admin token
can add jobs. Both come from the fleet vault, never from a file:
  ./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN,FLEET_DATABASE_URL -- \
      uv run --directory broker uvicorn fleet_broker.app:app
(or the image, with Compose: see compose.yaml).
The job store is PostgreSQL (ADR 0020): FLEET_DATABASE_URL is required and holds the password,
so it is never logged. Set FLEET_DOCS=1 to serve /docs locally.
"""

import hmac
import logging
import os
import time
from collections.abc import Callable, Collection
from contextlib import asynccontextmanager
from typing import Annotated, Literal, get_args

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from . import jsonlog, s3, telemetry
from .auth import bearer_matches
from .bodylimit import BodyLimit
from .ratelimit import TokenBucket, retry_after
from .s3 import ResultBucket
from .store import (
    POOL_TIMEOUT,
    JobStore,
    ResultKeyMismatch,
    is_unavailable,
    make_pool,
    result_key_for,
)
from .telemetry import Metrics

log = logging.getLogger("fleet_broker")

MIN_TOKEN_LENGTH = 32  # e.g. secrets.token_urlsafe(32) gives 43 characters
MAX_BODY_BYTES = 16 * 1024  # every request here is a few hundred bytes of JSON

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Covers Ollama tags ("llama3.1:8b-instruct-q4_K_M"), registry paths and digest pins ("m@sha256:...")
Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/+@-]+$")]
Token = Annotated[str, Field(min_length=1, max_length=128)]


DataClass = Literal["footage", "derived", "open"]  # GLOSSARY.md "Data class"
DATA_CLASSES = frozenset(get_args(DataClass))
MAX_LIST = 32  # capabilities and models a worker may declare in one lease request


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are an error, not ignored


class NewJob(Strict):
    job_id: Name
    input_sha256: Sha256Hex
    model: Name
    prompt_version: Name
    context_sha256: Sha256Hex | None = None  # pass 2: hash of the story context (ADR 0012)
    data_class: DataClass  # required: who may see this input (ADR 0015)


class LeaseIn(Strict):
    """What the worker can run. Its clearance is NOT here: the broker's config sets that."""

    capabilities: Annotated[list[Name], Field(min_length=1, max_length=MAX_LIST)]
    models: Annotated[list[Name], Field(min_length=1, max_length=MAX_LIST)]


class LeaseOut(BaseModel):
    job_id: str
    input_sha256: str
    model: str
    prompt_version: str
    context_sha256: str | None
    lease_token: str
    # a duration, never a deadline: workers time it on their own clock
    ttl_seconds: float
    attempts: int


class S3LeaseOut(LeaseOut):
    """The lease in S3 mode (ADR 0025): the same fields plus presigned download URLs. A separate
    model, so a local-mode lease has exactly the old fields and no nulls for the new ones."""

    input_url: str
    context_url: str | None = None


class LeaseRef(Strict):
    job_id: Name
    lease_token: Token


class UploadIn(LeaseRef):
    result_key: Sha256Hex  # only checked against the job's own key, never used to build a URL
    sha256: Sha256Hex  # of the result bytes (hex); S3 refuses a PUT whose bytes hash differently
    size: Annotated[int, Field(ge=1, le=2**53)]  # over the cap is a clean 413, not a schema 422


class UploadOut(BaseModel):
    url: str
    headers: dict[str, str]
    expires_in: int


class HeartbeatOut(BaseModel):
    ttl_seconds: float


class Completion(LeaseRef):
    result_key: Sha256Hex


def _bearer_checker(
    tokens: Collection[str],
    role: str,
    bucket: TokenBucket | None = None,
    on_limited: Callable[[], None] = lambda: None,
) -> Callable[..., None]:
    def check(authorization: Annotated[str, Header()] = "") -> None:
        if not bearer_matches(authorization, tokens):
            raise HTTPException(status_code=401, detail=f"bad or missing {role} token")
        # After authentication, so strangers can't use up the workers' allowance.
        wait = bucket.take() if bucket else 0.0
        if wait:
            on_limited()
            raise HTTPException(
                status_code=429, detail="rate limited", headers={"Retry-After": retry_after(wait)}
            )

    return check


def _token_sets(
    worker_token: str, admin_token: str, previous_worker: str, previous_admin: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The tokens each role accepts: the current one, plus the previous one during a rotation
    (docs/secrets.md). Every token gets the same checks, and no token may serve both roles."""
    workers = tuple(t for t in (worker_token, previous_worker) if t)
    admins = tuple(t for t in (admin_token, previous_admin) if t)
    for role, tokens in (("worker", workers), ("admin", admins)):
        for token in tokens:
            if not token.isascii():  # headers arrive latin-1 decoded: keep tokens plain ASCII
                raise ValueError(f"{role} token must be ASCII")
            if len(token) < MIN_TOKEN_LENGTH:
                raise ValueError(f"{role} token must be at least {MIN_TOKEN_LENGTH} characters")
    if any(hmac.compare_digest(w.encode(), a.encode()) for w in workers for a in admins):
        raise ValueError("worker and admin tokens must differ")
    return workers, admins


def create_app(
    database: str | ConnectionPool,
    worker_token: str,
    admin_token: str,
    clearance: Collection[str],
    ttl_seconds: float = 60.0,
    max_attempts: int = 3,
    clock: Callable[[], float] = time.time,
    docs: bool = False,
    pool_timeout: float = POOL_TIMEOUT,
    metrics: Metrics | None = None,
    tracing: Callable[[FastAPI], Callable[[], None]] | None = None,
    previous_worker_token: str = "",
    previous_admin_token: str = "",
    worker_rate: float = 20.0,
    worker_burst: int = 100,
    bucket: ResultBucket | None = None,
    max_result_bytes: int = s3.DEFAULT_MAX_RESULT_BYTES,
) -> FastAPI:
    """`database`: the PostgreSQL URL, or an open pool made by store.make_pool. The app owns the
    pool either way: its lifespan closes it, so shutdown (SIGTERM) releases the connections.
    `pool_timeout`: how long a request waits for a free connection before it gets a 503.
    `tracing` instruments the finished app and returns its flush (telemetry.trace_app).
    `previous_*_token`: also accepted while a rotation is under way (docs/secrets.md).
    `worker_rate`/`worker_burst`: the worker endpoints' token bucket (ratelimit.py). 20/s is
    far above a GPU fleet's need (a few requests per job, jobs take seconds) and far below a
    hot loop's.
    `bucket`: the results bucket (S3 mode, ADR 0025): leases carry presigned URLs, /upload-url
    exists, and /complete checks the stored object. None is local mode, exactly as before.
    `max_result_bytes`: the largest result /upload-url will sign for."""
    worker_tokens, admin_tokens = _token_sets(
        worker_token, admin_token, previous_worker_token, previous_admin_token
    )
    clearance = frozenset(clearance)
    metrics = metrics or Metrics()
    if not clearance or not clearance <= DATA_CLASSES:
        raise ValueError(
            f"worker clearance must be a non-empty subset of {sorted(DATA_CLASSES)}, "
            f"got {sorted(clearance)}"
        )

    # Opened here, at startup (a bad URL or an unreachable database stops the broker before it
    # takes traffic); closed by the lifespan below. Created after the checks above, so a refused
    # configuration leaves nothing open.
    pool = make_pool(database, timeout=pool_timeout) if isinstance(database, str) else database

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            # Metrics first: the last collection calls gauge callbacks that query the pool.
            try:
                metrics.shutdown()  # flush the last metrics window on SIGTERM
                stop_tracing()
            finally:
                pool.close()  # on SIGTERM too: connections end cleanly, not by a dropped socket

    # No public API description unless asked for (local development only).
    app = FastAPI(
        lifespan=lifespan,
        title="fleet broker",
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.pool = pool  # tests look at it; nothing else should
    # Create the schema once at startup (under an advisory lock: instances starting together
    # take turns). If it fails the broker doesn't start, and the pool doesn't leak.
    try:
        store = JobStore(pool, max_attempts)
    except BaseException:
        pool.close()
        raise

    @app.exception_handler(psycopg.Error)
    async def unavailable(request: Request, exc: psycopg.Error) -> Response:
        # A database that can't answer right now is transient: tell the caller to retry
        # (ADR 0020, ADR 0016). Anything else (a missing table, a bad query) is a real fault
        # and stays a 500. Logs the error's class and SQLSTATE only: connection errors can
        # quote the host and user, and the URL holds the password.
        if not is_unavailable(exc):
            raise exc
        log.warning(
            "database unavailable on %s %s: %s %s",
            request.method,
            request.url.path,
            type(exc).__name__,
            getattr(exc, "sqlstate", None),
        )
        metrics.busy()
        return Response(status_code=503, headers={"Retry-After": "1"})

    @app.exception_handler(s3.BucketUnavailable)
    async def bucket_unavailable(request: Request, exc: s3.BucketUnavailable) -> Response:
        # No credentials, no presign, or S3 would not answer (a 403 from HEAD included: loud, not
        # "missing"): transient from the worker's side, so it retries (like a 503 from the
        # database). The exception's type or S3 code only; nothing from the request.
        log.warning("results bucket unavailable on %s: %s", request.url.path, exc)
        metrics.busy()
        return JSONResponse(
            status_code=503,
            content={"detail": "storage unavailable", "code": "storage_unavailable"},
            headers={"Retry-After": "1"},
        )

    # Bodies without a Content-Length (chunked) are counted as they arrive (bodylimit.py).
    app.add_middleware(BodyLimit, limit=MAX_BODY_BYTES)

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        # Rejects declared oversize bodies before they're read; BodyLimit catches undeclared
        # (chunked) ones while reading, and the field limits below catch the rest.
        length = request.headers.get("content-length")
        if length is not None and (
            # isascii: str.isdigit() also accepts "²", which int() can't parse
            not (length.isascii() and length.isdigit()) or int(length) > MAX_BODY_BYTES
        ):
            return Response(status_code=413)
        return await call_next(request)

    def get_store() -> JobStore:
        # One store for the app: it holds no connection, each method borrows one from the pool.
        return store

    Store = Annotated[JobStore, Depends(get_store)]
    # Two buckets: heartbeats get their own, so a worker hot-looping on /lease (a bug, or a leaked
    # token) can't make the other workers' heartbeats fail and cost them their running jobs.
    polls = TokenBucket(worker_rate, worker_burst, clock)
    beats = TokenBucket(worker_rate, worker_burst, clock)
    worker = [Depends(_bearer_checker(worker_tokens, "worker", polls, metrics.limited))]
    heartbeater = [Depends(_bearer_checker(worker_tokens, "worker", beats, metrics.limited))]
    admin = [Depends(_bearer_checker(admin_tokens, "admin"))]

    @app.get("/healthz", status_code=204)
    def healthz() -> Response:
        # For Compose, Cloud Run and Kubernetes probes: "the process is up", nothing more.
        # No auth, so it must reveal nothing: no body, no version, no database status (ADR 0014).
        return Response(status_code=204)

    @app.post("/jobs", status_code=201, dependencies=admin)
    def add_job(job: NewJob, s: Store) -> None:
        s.add(
            job.job_id,
            job.input_sha256,
            job.model,
            job.prompt_version,
            job.context_sha256,
            job.data_class,
            now=clock(),
        )

    # response_model=None: one declared model would cut the S3 lease's extra fields; both are
    # built as models (validated) and sent as their dicts.
    @app.post("/lease", response_model=None, dependencies=worker)
    def lease(want: LeaseIn, s: Store):
        if bucket is not None:
            # Before the claim, which counts an attempt: an STS or metadata outage must not
            # walk every job toward the dead-letter limit.
            bucket.check()
        claim = s.claim(clock(), ttl_seconds, clearance, want.capabilities, want.models)
        if claim is None:
            return Response(status_code=204)
        metrics.leased(claim)
        fields = {
            "job_id": claim.job_id,
            "input_sha256": claim.input_sha256,
            "model": claim.model,
            "prompt_version": claim.prompt_version,
            "context_sha256": claim.context_sha256,
            "lease_token": claim.lease_token,
            "ttl_seconds": ttl_seconds,
            "attempts": claim.attempts,
        }
        if bucket is None:
            return JSONResponse(LeaseOut(**fields).model_dump())
        # Signed locally, for this job's own objects only, for whatever its data class: the
        # clearance filter already decided that this worker may hold the lease (ADR 0025).
        out = S3LeaseOut(
            **fields,
            input_url=bucket.get_url(s3.input_key(claim.input_sha256)),
            context_url=(
                bucket.get_url(s3.input_key(claim.context_sha256))
                if claim.context_sha256 is not None
                else None
            ),
        )
        return JSONResponse(out.model_dump(exclude_none=False))

    @app.post("/upload-url", dependencies=worker)
    def upload_url(up: UploadIn, s: Store):
        if bucket is None:
            # 404, not 409: 409 means "lease lost" to a worker (ADR 0007) and it would move on to
            # the next job; this broker simply has no such endpoint, which is a configuration fact.
            return JSONResponse(
                status_code=404,
                content={"detail": "this broker issues no presigned URLs", "code": s3.DISABLED},
            )
        if up.size > max_result_bytes:
            return JSONResponse(
                status_code=413,
                content={"detail": "result too large", "code": "result_too_large"},
            )
        bucket.check()  # credentials first: an outage is a 503 here, before anything is written
        try:
            key = s.holder_key(up.job_id, up.lease_token, up.result_key)
        except ResultKeyMismatch:
            return _refuse(
                "result_key_mismatch", "result_key is not this job's content address", up
            )
        if key is None:
            raise HTTPException(status_code=409, detail="lease lost")
        # The key is the one the broker recomputed from the job's row, not the caller's string.
        object_key = s3.result_object_key(key)
        # A result is already stored (an earlier attempt of this job, or a rerun after a crash):
        # the first valid one wins. Results are content-addressed by input, model and prompt, so
        # any valid output for them is acceptable (ADR 0007). Declare IT, so /complete passes, and
        # sign nothing: a PUT would be refused (If-None-Match) or replace a good result.
        stored = bucket.head(object_key)
        if stored is not None and stored.checksum_sha256 and 0 < stored.size <= max_result_bytes:
            declared = s.declare_result(
                up.job_id, up.lease_token, s3.hex_digest(stored.checksum_sha256), stored.size
            )
            if not declared:
                raise HTTPException(status_code=409, detail="lease lost")
            return JSONResponse({"already_stored": True})
        if not s.declare_result(up.job_id, up.lease_token, up.sha256, up.size):
            raise HTTPException(status_code=409, detail="lease lost")
        target = bucket.put_target(object_key, up.sha256, up.size)
        return UploadOut(url=target.url, headers=target.headers, expires_in=target.expires_in)

    @app.post("/heartbeat", dependencies=heartbeater)
    def heartbeat(ref: LeaseRef, s: Store) -> HeartbeatOut:
        if s.heartbeat(ref.job_id, ref.lease_token, clock(), ttl_seconds) is None:
            raise HTTPException(status_code=409, detail="lease lost")
        return HeartbeatOut(ttl_seconds=ttl_seconds)

    @app.post("/complete", status_code=204, dependencies=worker)
    def complete(done: Completion, s: Store):
        if bucket is not None:
            refused = _check_stored_result(done, s)
            if refused is not None:
                return refused
        try:
            accepted = s.complete(done.job_id, done.lease_token, done.result_key, now=clock())
        except ResultKeyMismatch:
            # Not the job's content address (ADR 0023). 422, not the 409 of a lost lease: the
            # worker must not read this as "someone else has it", and it is not transient.
            # The job id only: the key is the client's claim, not ours to log.
            return _refuse(
                "result_key_mismatch", "result_key is not this job's content address", done
            )
        if accepted is None:
            raise HTTPException(status_code=409, detail="lease lost")
        metrics.completed(accepted)
        return None

    def _refuse(code: str, detail: str, ref: LeaseRef) -> Response:
        # The job id only: the key and the hashes are the client's claims, not ours to log.
        log.warning("%s for job %s", code, ref.job_id)
        metrics.result_rejected()
        # "code" is for the worker: FastAPI's own 422s (a malformed body) carry none.
        return JSONResponse(status_code=422, content={"detail": detail, "code": code})

    def _check_stored_result(done: Completion, s: JobStore) -> Response | None:
        """S3 mode: before the job is recorded done, the object must be there and be what
        /upload-url signed for (ADR 0025). None means "go on to the store's own checks"."""
        facts = s.upload_facts(done.job_id, done.lease_token)
        if facts is None or facts["state"] == "done":
            return None  # not the holder (409 below), or a repeat of an already verified result
        if result_key_for(facts) != done.result_key:
            return None  # the store refuses it with the ADR 0023 code, before S3 is asked
        head = bucket.head(s3.result_object_key(done.result_key))
        if head is None:
            return _refuse("result_missing", "no result object is stored for this job", done)
        declared = (facts["result_sha256"], facts["result_size"])
        if (
            declared[0] is None
            or head.size > max_result_bytes
            or head.size != declared[1]
            or head.checksum_sha256 != s3.b64_digest(declared[0])
        ):
            return _refuse("result_mismatch", "the stored result is not what was declared", done)
        return None

    stop_tracing = tracing(app) if tracing else (lambda: None)
    return app


def _from_env() -> FastAPI | None:
    # Stripped: a secret store often returns "token\n", which no HTTP header can carry.
    worker_token = os.environ.get("FLEET_WORKER_TOKEN", "").strip()
    admin_token = os.environ.get("FLEET_ADMIN_TOKEN", "").strip()
    if not (worker_token and admin_token):
        return None  # imported by tests or tools: no app without both tokens
    # A real broker process: uvicorn has set up its loggers by now, so replace them with JSON.
    jsonlog.setup()
    # uvicorn's access log is one line per request with the caller's IP: personal data for every
    # volunteer, and nothing the request spans and metrics don't already say (ADR 0018).
    logging.getLogger("uvicorn.access").disabled = True
    # Required, no default: a forgotten setting must stop the broker, not widen access (ADR 0015).
    raw = os.environ.get("FLEET_WORKER_CLEARANCE", "")
    clearance = {c.strip() for c in raw.split(",") if c.strip()}
    if not clearance or not clearance <= DATA_CLASSES:
        raise ValueError(
            f"FLEET_WORKER_CLEARANCE must be a comma list from {sorted(DATA_CLASSES)}, got {raw!r}"
        )
    # Required too (ADR 0020). Never echoed: it holds the database password.
    database_url = os.environ.get("FLEET_DATABASE_URL", "")
    if not database_url:
        raise ValueError("FLEET_DATABASE_URL is required (a postgresql:// URL)")
    # S3 mode (ADR 0025) is all-or-nothing; a half-set deployment stops here, before any pool.
    bucket = s3.bucket_from_env(os.environ)
    max_result_bytes = s3.max_result_bytes_from_env(os.environ)
    pool = make_pool(database_url)
    flush_logs = telemetry.export_logs()  # a no-op without the [otel] extra and an endpoint
    # Set only while a rotation is under way (docs/secrets.md "Rotating broker tokens").
    previous_worker = os.environ.get("FLEET_WORKER_TOKEN_PREVIOUS", "").strip()
    previous_admin = os.environ.get("FLEET_ADMIN_TOKEN_PREVIOUS", "").strip()
    worker_tokens = tuple(t for t in (worker_token, previous_worker) if t)

    def tracing(a: FastAPI) -> Callable[[], None]:
        stop_traces = telemetry.trace_app(a, worker_tokens=worker_tokens)

        def stop() -> None:  # on shutdown: the last spans, then the last log records
            stop_traces()
            flush_logs()

        return stop

    try:
        return create_app(
            pool,
            worker_token,
            admin_token,
            clearance,
            docs=os.environ.get("FLEET_DOCS") == "1",
            # The metrics thread reads the gauges through the app's own pool (the schema is made
            # by create_app before the first export).
            metrics=telemetry.setup(JobStore(pool, migrate=False), time.time),
            tracing=tracing,
            previous_worker_token=previous_worker,
            previous_admin_token=previous_admin,
            # Optional overrides of the worker rate limit; TokenBucket refuses non-positive values.
            worker_rate=float(os.environ.get("FLEET_WORKER_RATE", "20")),
            worker_burst=int(os.environ.get("FLEET_WORKER_BURST", "100")),
            bucket=bucket,
            max_result_bytes=max_result_bytes,
        )
    except BaseException:
        pool.close()  # a refused configuration (weak token, bad rate) leaves no pool or threads
        raise


app = _from_env()
