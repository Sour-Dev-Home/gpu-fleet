"""`fleet-enqueue`: add jobs to the broker through its own API, with the admin token.

  fleet-enqueue --demo 20             20 demo jobs for the smoke runner
  fleet-enqueue --demo 20 --run b     the same 20 inputs under new job ids (run label "b")

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
import sys
import time
import urllib.error
import urllib.request

DEMO_MODEL = "qwen3:8b"
DEMO_PROMPT_VERSION = "smoke-v1"


def demo_jobs(count: int, run: str) -> list[dict[str, str]]:
    return [
        {
            "job_id": f"demo-{run}-{i}",
            "input_sha256": hashlib.sha256(f"demo-input-{i}".encode()).hexdigest(),
            "model": DEMO_MODEL,
            "prompt_version": DEMO_PROMPT_VERSION,
        }
        for i in range(count)
    ]


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
        return e.code


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    p = argparse.ArgumentParser(prog="fleet-enqueue", description="Add jobs to the broker.")
    p.add_argument("--demo", type=int, required=True, metavar="N", help="add N demo jobs")
    p.add_argument("--run", default=time.strftime("%Y%m%d-%H%M%S"), help="job id label")
    args = p.parse_args(argv)

    token = env.get("FLEET_ADMIN_TOKEN")
    if not token:
        print("fleet-enqueue: missing FLEET_ADMIN_TOKEN", file=sys.stderr)
        return 2
    base_url = env.get("FLEET_BROKER_URL", "http://127.0.0.1:8000")

    statuses = [post_job(base_url, token, job) for job in demo_jobs(args.demo, args.run)]
    added = statuses.count(201)
    print(f"fleet-enqueue: {added}/{len(statuses)} jobs added (run {args.run})")
    if added != len(statuses):
        print(f"fleet-enqueue: unexpected statuses {sorted(set(statuses))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
