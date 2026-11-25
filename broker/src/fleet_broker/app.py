"""The broker's HTTP API: lease, heartbeat, complete (for workers) and add jobs (admin).

Status codes carry the store's answers:
- 200 with a body: done.
- 204 from /lease: no work right now (normal; the worker backs off).
- 409 from /heartbeat or /complete: the caller no longer owns this job (stale token).
- 401: missing or wrong bearer token.

Run locally (the token comes from the fleet vault, never from a file):
  ./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN -- uv run --directory broker --with uvicorn uvicorn fleet_broker.app:app
"""

import hmac
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from .store import JobStore


class NewJob(BaseModel):
    job_id: str
    input_sha256: str
    model: str
    prompt_version: str


class LeaseOut(BaseModel):
    job_id: str
    input_sha256: str
    model: str
    prompt_version: str
    lease_token: str
    # a duration, never a deadline: workers time it on their own clock
    ttl_seconds: float
    attempts: int


class LeaseRef(BaseModel):
    job_id: str
    lease_token: str


class HeartbeatOut(BaseModel):
    ttl_seconds: float


class Completion(LeaseRef):
    result_key: str


def create_app(
    db_path: str | Path,
    worker_token: str,
    ttl_seconds: float = 60.0,
    max_attempts: int = 3,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    app = FastAPI(title="fleet broker")
    JobStore(db_path, max_attempts).close()  # create the schema once at startup

    def store() -> Iterator[JobStore]:
        # One SQLite connection per request: requests run on a thread pool, and a
        # connection must not be shared across threads. Opening one costs well under 1 ms.
        s = JobStore(db_path, max_attempts)
        try:
            yield s
        finally:
            s.close()

    Store = Annotated[JobStore, Depends(store)]

    def authorized(authorization: Annotated[str, Header()] = "") -> None:
        # compare_digest takes the same time whether the first or the last character
        # differs, so the token can't be guessed one character at a time from timings.
        if not hmac.compare_digest(authorization, f"Bearer {worker_token}"):
            raise HTTPException(status_code=401, detail="bad or missing worker token")

    auth = [Depends(authorized)]

    @app.post("/jobs", status_code=201, dependencies=auth)
    def add_job(job: NewJob, s: Store) -> None:
        s.add(job.job_id, job.input_sha256, job.model, job.prompt_version)

    @app.post("/lease", response_model=LeaseOut, dependencies=auth)
    def lease(s: Store):
        claim = s.claim(clock(), ttl_seconds)
        if claim is None:
            return Response(status_code=204)
        return LeaseOut(
            job_id=claim.job_id,
            input_sha256=claim.input_sha256,
            model=claim.model,
            prompt_version=claim.prompt_version,
            lease_token=claim.lease_token,
            ttl_seconds=ttl_seconds,
            attempts=claim.attempts,
        )

    @app.post("/heartbeat", dependencies=auth)
    def heartbeat(ref: LeaseRef, s: Store) -> HeartbeatOut:
        if s.heartbeat(ref.job_id, ref.lease_token, clock(), ttl_seconds) is None:
            raise HTTPException(status_code=409, detail="lease lost")
        return HeartbeatOut(ttl_seconds=ttl_seconds)

    @app.post("/complete", status_code=204, dependencies=auth)
    def complete(done: Completion, s: Store) -> None:
        if not s.complete(done.job_id, done.lease_token, done.result_key):
            raise HTTPException(status_code=409, detail="lease lost or different result")

    return app


def _from_env() -> FastAPI | None:
    token = os.environ.get("FLEET_WORKER_TOKEN")
    if not token:
        return None  # imported by tests or tools: no app without a token
    return create_app(os.environ.get("FLEET_DB", "broker.db"), token)


app = _from_env()
