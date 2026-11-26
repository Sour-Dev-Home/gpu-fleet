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
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from . import jsonlog, telemetry
from .auth import bearer_matches
from .bodylimit import BodyLimit
from .ratelimit import TokenBucket, retry_after
from .store import POOL_TIMEOUT, JobStore, ResultKeyMismatch, is_unavailable, make_pool
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


class LeaseRef(Strict):
    job_id: Name
    lease_token: Token


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
) -> FastAPI:
    """`database`: the PostgreSQL URL, or an open pool made by store.make_pool. The app owns the
    pool either way: its lifespan closes it, so shutdown (SIGTERM) releases the connections.
    `pool_timeout`: how long a request waits for a free connection before it gets a 503.
    `tracing` instruments the finished app and returns its flush (telemetry.trace_app).
    `previous_*_token`: also accepted while a rotation is under way (docs/secrets.md).
    `worker_rate`/`worker_burst`: the worker endpoints' token bucket (ratelimit.py). 20/s is
    far above a GPU fleet's need (a few requests per job, jobs take seconds) and far below a
    hot loop's."""
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

    @app.post("/lease", response_model=LeaseOut, dependencies=worker)
    def lease(want: LeaseIn, s: Store):
        claim = s.claim(clock(), ttl_seconds, clearance, want.capabilities, want.models)
        if claim is None:
            return Response(status_code=204)
        metrics.leased(claim)
        return LeaseOut(
            job_id=claim.job_id,
            input_sha256=claim.input_sha256,
            model=claim.model,
            prompt_version=claim.prompt_version,
            context_sha256=claim.context_sha256,
            lease_token=claim.lease_token,
            ttl_seconds=ttl_seconds,
            attempts=claim.attempts,
        )

    @app.post("/heartbeat", dependencies=heartbeater)
    def heartbeat(ref: LeaseRef, s: Store) -> HeartbeatOut:
        if s.heartbeat(ref.job_id, ref.lease_token, clock(), ttl_seconds) is None:
            raise HTTPException(status_code=409, detail="lease lost")
        return HeartbeatOut(ttl_seconds=ttl_seconds)

    @app.post("/complete", status_code=204, dependencies=worker)
    def complete(done: Completion, s: Store) -> None:
        try:
            accepted = s.complete(done.job_id, done.lease_token, done.result_key, now=clock())
        except ResultKeyMismatch:
            # Not the job's content address (ADR 0023). 422, not the 409 of a lost lease: the
            # worker must not read this as "someone else has it", and it is not transient.
            # The job id only: the key is the client's claim, not ours to log.
            log.warning("result key refused for job %s", done.job_id)
            metrics.result_rejected()
            raise HTTPException(
                status_code=422, detail="result_key is not this job's content address"
            ) from None
        if accepted is None:
            raise HTTPException(status_code=409, detail="lease lost")
        metrics.completed(accepted)

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
        )
    except BaseException:
        pool.close()  # a refused configuration (weak token, bad rate) leaves no pool or threads
        raise


app = _from_env()
