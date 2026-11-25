"""Exponential backoff with full jitter (ADR 0008).

The n-th wait is a random time in [0, min(cap, base * 2**n)). The randomness ("full jitter")
spreads out workers that would otherwise retry in lockstep, e.g. all at once after a
broker restart. Any success calls reset().
"""

import random
from collections.abc import Callable


class Backoff:
    def __init__(
        self,
        base: float = 1.0,
        cap: float = 60.0,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.base = base
        self.cap = cap
        self.rng = rng  # returns a float in [0, 1); injected so tests are deterministic
        self.failures = 0

    def ceiling(self) -> float:
        # min() before the power would overflow; 2**30 seconds is far past any cap anyway
        return min(self.cap, self.base * 2 ** min(self.failures, 30))

    def next_delay(self) -> float:
        delay = self.rng() * self.ceiling()
        self.failures += 1
        return delay

    def reset(self) -> None:
        self.failures = 0
