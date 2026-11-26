"""`fleet-enqueue`: add jobs to the broker through its own API, with the admin token.

  fleet-enqueue --demo 20                           20 demo jobs for the echo runner (echo-v1)
  fleet-enqueue --demo 20 --prompt-version smoke-v1 20 demo jobs for the smoke runner
  fleet-enqueue --demo 20 --run b                  the same 20 inputs under new job ids ("b")
  --model picks the jobs' model (default "echo" for echo-v1, else qwen3:8b).
  --data-class footage|derived|open (default open): who may see the inputs (ADR 0015).
  A job is leased only to a worker whose clearance covers its class and which declared its
  prompt version and model; others stay queued.

Reads FLEET_ADMIN_TOKEN and FLEET_BROKER_URL (default http://127.0.0.1:8000). Plain http is
refused unless the host is loopback or FLEET_ALLOW_HTTP=1; redirects are never followed. In Compose it
runs inside the broker container, which already holds the admin token (ADR 0014):
  docker compose run --rm broker fleet-enqueue --demo 20

Demo inputs are numbered, so input i always hashes the same: a second run under a new label
gets new jobs whose Results already exist, and workers complete them as "cached".
Standard library only, so the broker image needs no HTTP client for it.
"""

import argparse
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEMO_MODEL = "qwen3:8b"
# echo-v1: matches Compose's default runner (echo), so the default demo just works.
DEMO_PROMPT_VERSION = "echo-v1"
# The echo runner declares this placeholder model; jobs for it must name it (ADR 0015).
ECHO_MODEL = "echo"
DATA_CLASSES = ("footage", "derived", "open")
DEMO_DATA_CLASS = "open"  # demo inputs are made-up numbers, safe for any worker


def default_model(prompt_version: str) -> str:
    return ECHO_MODEL if prompt_version == DEMO_PROMPT_VERSION else DEMO_MODEL


def demo_jobs(
    count: int,
    run: str,
    model: str | None = None,
    prompt_version: str = DEMO_PROMPT_VERSION,
    data_class: str = DEMO_DATA_CLASS,
) -> list[dict[str, str]]:
    model = model or default_model(prompt_version)
    return [
        {
            "job_id": f"demo-{run}-{i}",
            "input_sha256": hashlib.sha256(f"demo-input-{i}".encode()).hexdigest(),
            "model": model,
            "prompt_version": prompt_version,
            "data_class": data_class,
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


def post_job_with_retries(
    base_url: str, token: str, job: dict[str, str], allow_http: bool = False
) -> int:
    """POST /jobs, retrying 503 and connection errors. Safe: the endpoint is INSERT OR IGNORE.
    4xx is never retried. Returns the last status, or 0 if no attempt got an answer."""
    status = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status = post_job(base_url, token, job, allow_http)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            status = 0
        if status not in (0, 503):
            return status
        if attempt < MAX_ATTEMPTS:
            _sleep(backoff(attempt, getattr(status, "retry_after", None)))
    return status


def _is_loopback_ip(host: str) -> bool:
    """127.0.0.0/8 and ::1 in any spelling; hostnames like 127.0.0.1.evil.com are not IPs."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_SERVICE_NAME = re.compile(r"[a-z][a-z0-9-]*")  # one DNS label, not an IP in any spelling


def check_url(base_url: str, allow_http: bool) -> None:
    """The admin token rides on every request: never in cleartext to a remote host.
    Plain http is fine for loopback, or, with the opt-in, for a single-label name such as
    Compose's http://broker:8000. Userinfo, backslashes and %-escapes are refused: urllib and
    urlsplit disagree about the host in `http://evil.com\\@127.0.0.1`, and the guard must see
    the host urllib connects to."""
    url = urllib.parse.urlsplit(base_url)
    if any(c in url.netloc for c in "@\\%"):
        raise ValueError(
            "refusing a broker URL with userinfo, a backslash or a %-escape in its host"
        )
    host = (url.hostname or "").lower()
    loopback = host == "localhost" or _is_loopback_ip(host)
    service = _SERVICE_NAME.fullmatch(host) is not None
    if url.scheme == "https" or (url.scheme == "http" and (loopback or (allow_http and service))):
        return
    raise ValueError(
        f"refusing to send the admin token over {url.scheme}://{host} "
        "(use https, or set FLEET_ALLOW_HTTP=1 for a trusted private network)"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error, never followed: urllib would resend the Authorization header."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _open(req: urllib.request.Request, timeout: float):  # patched in tests
    return _opener.open(req, timeout=timeout)


def post_job(base_url: str, token: str, job: dict[str, str], allow_http: bool = False) -> int:
    check_url(base_url, allow_http)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/jobs",
        data=json.dumps(job).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _open(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        status = Status(e.code)
        status.retry_after = parse_retry_after(e.headers.get("Retry-After", ""))
        return status
    except ValueError:
        # http.client's "Invalid header value b'Bearer <token>'" would put the token in a traceback.
        raise ValueError("the request could not be sent (invalid header or URL)") from None


def parse_retry_after(header: str) -> float | None:
    """Seconds from a Retry-After header, clamped to [0, BACKOFF_CAP]; None when absent,
    an HTTP-date (not parsed: plain backoff applies), or not finite ("inf" would crash sleep)."""
    try:
        v = float(header)
    except ValueError:
        return None
    return min(max(v, 0.0), BACKOFF_CAP) if math.isfinite(v) else None


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
    p.add_argument(
        "--model",
        help=f"model for the jobs (default {ECHO_MODEL} for echo-v1, else {DEMO_MODEL})",
    )
    p.add_argument(
        "--data-class",
        choices=DATA_CLASSES,
        default=DEMO_DATA_CLASS,
        help=f"who may see the inputs (default {DEMO_DATA_CLASS} for --demo)",
    )
    args = p.parse_args(argv)
    model = args.model or default_model(args.prompt_version)

    token = env.get("FLEET_ADMIN_TOKEN", "").strip()  # a secret store may add "\n"
    if not token:
        print("fleet-enqueue: missing FLEET_ADMIN_TOKEN", file=sys.stderr)
        return 2
    if not (token.isascii() and token.isprintable() and " " not in token):
        # Never echoed: it is a secret, and an HTTP header cannot carry these characters.
        print("fleet-enqueue: FLEET_ADMIN_TOKEN has spaces or control characters", file=sys.stderr)
        return 2
    base_url = env.get("FLEET_BROKER_URL", "http://127.0.0.1:8000")
    allow_http = env.get("FLEET_ALLOW_HTTP") == "1"  # the one place the opt-in is read
    try:
        check_url(base_url, allow_http)
        statuses = [
            post_job_with_retries(base_url, token, job, allow_http)
            for job in demo_jobs(args.demo, args.run, model, args.prompt_version, args.data_class)
        ]
    except ValueError as e:  # messages here never contain the token
        print(f"fleet-enqueue: {e}", file=sys.stderr)
        return 2
    added = statuses.count(201)
    print(f"fleet-enqueue: {added}/{len(statuses)} jobs added (run {args.run})")
    if added != len(statuses):
        print(f"fleet-enqueue: unexpected statuses {sorted(set(statuses))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
