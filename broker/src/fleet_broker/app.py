"""The broker's HTTP API: lease, heartbeat, complete (workers) and add jobs (admin).

Status codes carry the store's answers (ADR 0007):
- 200 with a body: done.
- 204 from /lease: no work right now (normal; the worker backs off).
- 409 from /heartbeat or /complete: the caller no longer owns this job (stale token).
- 401: missing or wrong bearer token. 413: request body too large. 422: malformed fields.

Two tokens (ADR 0011): workers can only lease, heartbeat and complete; only the admin token
can add jobs. Both come from the fleet vault, never from a file:
  ./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- \
      uv run --directory broker uvicorn fleet_broker.app:app
(or the image, with Compose: see compose.yaml).
The database defaults to data/broker.db (git-ignored). Set FLEET_DOCS=1 to serve /docs locally.
"""

import hmac
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, get_args

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import jsonlog, telemetry
from .store import JobStore
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


def _bearer_checker(token: str, role: str) -> Callable[..., None]:
    expected = f"Bearer {token}".encode()

    def check(authorization: Annotated[str, Header()] = "") -> None:
        # Compare bytes: compare_digest raises TypeError on non-ASCII str, which would turn a
        # junk header into a 500. Constant time, so timings don't reveal the token.
        # Starlette decodes headers as latin-1, so encoding back with latin-1 is lossless.
        if not hmac.compare_digest(authorization.encode("latin-1"), expected):
            raise HTTPException(status_code=401, detail=f"bad or missing {role} token")

    return check


def create_app(
    db_path: str | Path,
    worker_token: str,
    admin_token: str,
    clearance: Collection[str],
    ttl_seconds: float = 60.0,
    max_attempts: int = 3,
    clock: Callable[[], float] = time.time,
    docs: bool = False,
    busy_timeout: float = 5.0,
    metrics: Metrics | None = None,
    tracing: Callable[[FastAPI], Callable[[], None]] | None = None,
) -> FastAPI:
    """`tracing` instruments the finished app and returns its flush (telemetry.trace_app)."""
    for name, token in (("worker", worker_token), ("admin", admin_token)):
        if not token.isascii():  # headers arrive latin-1 decoded: keep tokens plain ASCII
            raise ValueError(f"{name} token must be ASCII")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"{name} token must be at least {MIN_TOKEN_LENGTH} characters")
    if hmac.compare_digest(worker_token.encode(), admin_token.encode()):
        raise ValueError("worker and admin tokens must differ")
    clearance = frozenset(clearance)
    metrics = metrics or Metrics()
    if not clearance or not clearance <= DATA_CLASSES:
        raise ValueError(
            f"worker clearance must be a non-empty subset of {sorted(DATA_CLASSES)}, "
            f"got {sorted(clearance)}"
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        metrics.shutdown()  # flush the last metrics window on SIGTERM
        stop_tracing()

    # No public API description unless asked for (local development only).
    app = FastAPI(
        lifespan=lifespan,
        title="fleet broker",
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    # Create the schema and switch to WAL once at startup.
    JobStore(db_path, max_attempts, busy_timeout, wal=True).close()

    @app.exception_handler(sqlite3.OperationalError)
    async def busy(request: Request, exc: sqlite3.OperationalError) -> Response:
        # Lock starvation is transient: tell the caller to retry (ADR 0016). Anything else
        # (a missing table, a corrupt file) is a real fault and stays a 500.
        # SQLITE_BUSY (5): another connection holds the lock, so waiting helps. SQLITE_LOCKED (6)
        # is a conflict inside one connection, a bug that retrying can't fix. The primary code is
        # the low byte (extended codes add flags above it). Without a code, match the BUSY text.
        code = getattr(exc, "sqlite_errorcode", None)
        if (
            (code & 0xFF == sqlite3.SQLITE_BUSY)
            if code is not None
            else (str(exc) == "database is locked")
        ):
            log.warning("database busy on %s %s: %s", request.method, request.url.path, exc)
            metrics.busy()
            return Response(status_code=503, headers={"Retry-After": "1"})
        raise exc

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        # Rejects declared oversize bodies before they're read. Field limits below catch the
        # rest; the public deployment adds a gateway limit too (docs/security.md).
        length = request.headers.get("content-length")
        if length is not None and (
            # isascii: str.isdigit() also accepts "²", which int() can't parse
            not (length.isascii() and length.isdigit()) or int(length) > MAX_BODY_BYTES
        ):
            return Response(status_code=413)
        return await call_next(request)

    def store() -> Iterator[JobStore]:
        # One SQLite connection per request, never used by two threads at once (setup, endpoint
        # and teardown may run on different pool threads, but in turn). Opening one costs < 1 ms.
        s = JobStore(db_path, max_attempts, busy_timeout)
        try:
            yield s
        finally:
            s.close()

    Store = Annotated[JobStore, Depends(store)]
    worker = [Depends(_bearer_checker(worker_token, "worker"))]
    admin = [Depends(_bearer_checker(admin_token, "admin"))]

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

    @app.post("/heartbeat", dependencies=worker)
    def heartbeat(ref: LeaseRef, s: Store) -> HeartbeatOut:
        if s.heartbeat(ref.job_id, ref.lease_token, clock(), ttl_seconds) is None:
            raise HTTPException(status_code=409, detail="lease lost")
        return HeartbeatOut(ttl_seconds=ttl_seconds)

    @app.post("/complete", status_code=204, dependencies=worker)
    def complete(done: Completion, s: Store) -> None:
        accepted = s.complete(done.job_id, done.lease_token, done.result_key, now=clock())
        if accepted is None:
            raise HTTPException(status_code=409, detail="lease lost or different result")
        metrics.completed(accepted)

    stop_tracing = tracing(app) if tracing else (lambda: None)
    return app


def _from_env() -> FastAPI | None:
    worker_token = os.environ.get("FLEET_WORKER_TOKEN")
    admin_token = os.environ.get("FLEET_ADMIN_TOKEN")
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
    db = Path(os.environ.get("FLEET_DB", "data/broker.db"))
    db.parent.mkdir(parents=True, exist_ok=True)
    return create_app(
        db,
        worker_token,
        admin_token,
        clearance,
        docs=os.environ.get("FLEET_DOCS") == "1",
        metrics=telemetry.setup(db, time.time),
        tracing=lambda a: telemetry.trace_app(a, worker_token=worker_token),
    )


app = _from_env()
