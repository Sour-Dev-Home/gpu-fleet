"""`fleet-enqueue`: add jobs to the broker through its own API, with the admin token.

  fleet-enqueue --demo 20                           20 demo jobs for the echo runner (echo-v1)
  fleet-enqueue --demo 20 --prompt-version smoke-v1 20 demo jobs for the smoke runner
  fleet-enqueue --demo 20 --run b                  the same 20 inputs under new job ids ("b")
  --model picks the jobs' model (default qwen3:8b). Each runner implements fixed prompt
  versions; a worker stops on a job for another one (ADR 0014 amendment).

Reads FLEET_ADMIN_TOKEN and FLEET_BROKER_URL (default http://127.0.0.1:8000). In Compose it
runs inside the broker container, which already holds the admin token (ADR 0014):
  docker compose run --rm broker fleet-enqueue --demo 20

Demo inputs are numbered, so input i always hashes the same: a second run under a new label
gets new jobs whose Results already exist, and workers complete them as "cached".
Standard library only, so the broker image needs no HTTP client for it.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

DEMO_MODEL = "qwen3:8b"
# echo-v1: matches Compose's default runner (echo), so the default demo just works.
DEMO_PROMPT_VERSION = "echo-v1"


def demo_jobs(
    count: int,
    run: str,
    model: str = DEMO_MODEL,
    prompt_version: str = DEMO_PROMPT_VERSION,
) -> list[dict[str, str]]:
    return [
        {
            "job_id": f"demo-{run}-{i}",
            "input_sha256": hashlib.sha256(f"demo-input-{i}".encode()).hexdigest(),
            "model": model,
            "prompt_version": prompt_version,
        }
        for i in range(count)
    ]


MAX_ATTEMPTS = 5
BACKOFF_BASE = 0.5  # seconds; doubles per attempt
BACKOFF_CAP = 8.0
_sleep = time.sleep  # injectable for tests
_random = random.random


class Status(int):
    """An HTTP status that also remembers the response's Retry-After seconds, if any."""

    retry_after: float | None = None


def backoff(attempt: int, retry_after: float | None, rand=None) -> float:
    """Seconds to wait after failed attempt number `attempt` (1-based): full jitter, so
    concurrent clients spread out, but never less than the server's Retry-After."""
    rand = rand or _random
    wait = rand() * min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempt - 1))
    return max(wait, retry_after or 0.0)


def post_job_with_retries(base_url: str, token: str, job: dict[str, str]) -> int:
    """POST /jobs, retrying 503 and connection errors. Safe: the endpoint is INSERT OR IGNORE.
    4xx is never retried. Returns the last status, or 0 if no attempt got an answer."""
    status = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status = post_job(base_url, token, job)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            status = 0
        if status not in (0, 503):
            return status
        if attempt < MAX_ATTEMPTS:
            _sleep(backoff(attempt, getattr(status, "retry_after", None)))
    return status


def post_job(base_url: str, token: str, job: dict[str, str]) -> int:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/jobs",
        data=json.dumps(job).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        status = Status(e.code)
        try:
            status.retry_after = float(e.headers.get("Retry-After", ""))
        except ValueError:
            pass  # absent, or an HTTP-date we don't parse: plain backoff applies
        return status


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    p = argparse.ArgumentParser(prog="fleet-enqueue", description="Add jobs to the broker.")
    p.add_argument("--demo", type=int, required=True, metavar="N", help="add N demo jobs")
    p.add_argument("--run", default=time.strftime("%Y%m%d-%H%M%S"), help="job id label")
    p.add_argument(
        "--prompt-version",
        default=DEMO_PROMPT_VERSION,
        help="must be one the workers' runner implements: echo-v1 (default) or smoke-v1",
    )
    p.add_argument("--model", default=DEMO_MODEL, help=f"model for the jobs (default {DEMO_MODEL})")
    args = p.parse_args(argv)

    token = env.get("FLEET_ADMIN_TOKEN")
    if not token:
        print("fleet-enqueue: missing FLEET_ADMIN_TOKEN", file=sys.stderr)
        return 2
    base_url = env.get("FLEET_BROKER_URL", "http://127.0.0.1:8000")

    statuses = [
        post_job_with_retries(base_url, token, job)
        for job in demo_jobs(args.demo, args.run, args.model, args.prompt_version)
    ]
    added = statuses.count(201)
    print(f"fleet-enqueue: {added}/{len(statuses)} jobs added (run {args.run})")
    if added != len(statuses):
        print(f"fleet-enqueue: unexpected statuses {sorted(set(statuses))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
