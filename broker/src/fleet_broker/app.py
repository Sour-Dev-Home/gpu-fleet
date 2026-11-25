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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .store import JobStore

log = logging.getLogger("fleet_broker")

MIN_TOKEN_LENGTH = 32  # e.g. secrets.token_urlsafe(32) gives 43 characters
MAX_BODY_BYTES = 16 * 1024  # every request here is a few hundred bytes of JSON

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Covers Ollama tags ("llama3.1:8b-instruct-q4_K_M"), registry paths and digest pins ("m@sha256:...")
Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/+@-]+$")]
Token = Annotated[str, Field(min_length=1, max_length=128)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are an error, not ignored


class NewJob(Strict):
    job_id: Name
    input_sha256: Sha256Hex
    model: Name
    prompt_version: Name
    context_sha256: Sha256Hex | None = None  # pass 2: hash of the story context (ADR 0012)


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
    ttl_seconds: float = 60.0,
    max_attempts: int = 3,
    clock: Callable[[], float] = time.time,
    docs: bool = False,
    busy_timeout: float = 5.0,
) -> FastAPI:
    for name, token in (("worker", worker_token), ("admin", admin_token)):
        if not token.isascii():  # headers arrive latin-1 decoded: keep tokens plain ASCII
            raise ValueError(f"{name} token must be ASCII")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"{name} token must be at least {MIN_TOKEN_LENGTH} characters")
    if hmac.compare_digest(worker_token.encode(), admin_token.encode()):
        raise ValueError("worker and admin tokens must differ")

    # No public API description unless asked for (local development only).
    app = FastAPI(
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
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            log.warning("database busy on %s %s: %s", request.method, request.url.path, exc)
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
        # One SQLite connection per request: requests run on a thread pool, and a
        # connection must not be shared across threads. Opening one costs well under 1 ms.
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
        s.add(job.job_id, job.input_sha256, job.model, job.prompt_version, job.context_sha256)

    @app.post("/lease", response_model=LeaseOut, dependencies=worker)
    def lease(s: Store):
        claim = s.claim(clock(), ttl_seconds)
        if claim is None:
            return Response(status_code=204)
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
        if not s.complete(done.job_id, done.lease_token, done.result_key):
            raise HTTPException(status_code=409, detail="lease lost or different result")

    return app


def _from_env() -> FastAPI | None:
    worker_token = os.environ.get("FLEET_WORKER_TOKEN")
    admin_token = os.environ.get("FLEET_ADMIN_TOKEN")
    if not (worker_token and admin_token):
        return None  # imported by tests or tools: no app without both tokens
    db = Path(os.environ.get("FLEET_DB", "data/broker.db"))
    db.parent.mkdir(parents=True, exist_ok=True)
    return create_app(db, worker_token, admin_token, docs=os.environ.get("FLEET_DOCS") == "1")


app = _from_env()
