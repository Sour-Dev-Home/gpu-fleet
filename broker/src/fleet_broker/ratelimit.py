"""A token bucket for the worker endpoints (docs/security.md, before phase 4).

The bucket holds up to `burst` tokens and refills at `rate` per second; each request takes one.
So a burst of `burst` requests goes through at once, then `rate` per second.

It's per broker instance, in memory: with Cloud Run's max-instances cap at N, the fleet-wide
limit is up to N x rate. That's the point: the cap bounds the bill, the bucket bounds each
instance's thread pool. One bucket per role today (workers share one token); per-worker
buckets come with per-worker tokens in phase 10.
"""

import math
import threading
from collections.abc import Callable

MIN_RATE, MAX_RATE = 0.01, 10_000.0  # per second
MAX_BURST = 100_000


class TokenBucket:
    def __init__(self, rate: float, burst: int, clock: Callable[[], float]) -> None:
        # A range check, written as "inside" rather than "rate <= 0 is bad": every comparison
        # with nan is False, so `nan <= 0` would pass, and with nan or inf the bucket never
        # empties ("FLEET_WORKER_RATE=inf" would silently switch the limit off). Here nan fails
        # both bounds and inf the upper one. A rate near zero would overflow Retry-After.
        if not (MIN_RATE <= rate <= MAX_RATE and 1 <= burst <= MAX_BURST):
            raise ValueError(
                f"rate must be within {MIN_RATE}-{MAX_RATE}/s and burst within 1-{MAX_BURST},"
                f" got rate={rate}, burst={burst}"
            )
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = float(burst)
        self.updated = clock()
        # FastAPI runs sync endpoints on a thread pool: refill-and-take must be one step.
        self.lock = threading.Lock()

    def take(self) -> float:
        """Take a token. Returns 0 if allowed, else the seconds until one is available."""
        with self.lock:
            now = self.clock()
            # max(0, ...): a wall clock that steps back must not drain the bucket
            elapsed = max(0.0, now - self.updated)
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            return (1 - self.tokens) / self.rate


def retry_after(wait: float) -> str:
    """Retry-After takes whole seconds; round up so a client that obeys it gets a token."""
    return str(max(1, math.ceil(wait)))
