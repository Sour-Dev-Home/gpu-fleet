"""Which queued jobs could a worker take? (fleet.jobs.unclaimable, ADR 0026, Q84.)

The broker remembers what each lease request declared (its models and capabilities) for ten
minutes. A waiting job is unclaimable when no request in that window declared both its model
(in `models`) and its prompt version (in `capabilities`) in the same request, or when its data
class is outside the broker's clearance.

In memory, per instance, and that is acceptable only because the broker runs at most one
instance (max_instance_count = 1, ADR 0021): every worker polls the same process, so it sees
every declaration. With two or more instances a worker polls only one of them, and each
instance would call jobs unclaimable that a worker polling its sibling can take: false alarms,
worst after a scale-up. What changes then: keep the declarations in the database (a small table
of (models, capabilities, last_seen), written on lease) so every instance reads the same set.

Clearance is broker-wide: it comes from the broker's configuration, not from the request, and
every instance of one deployment has the same value. So a job whose class is outside it can never
be leased by anyone who reaches this broker, whatever workers declare: unclaimable, always.
"""

import threading
from collections.abc import Callable, Collection, Iterable, Mapping

WINDOW_SECONDS = 600.0  # how long a declaration counts
MAX_SERIES = 50  # label sets reported, "other" included
OTHER = "other"
# Distinct (models, capabilities) pairs remembered. Real fleets have a handful; only a worker
# token holder can add entries, but a loop varying its lists must not grow the process.
MAX_REMEMBERED = 1000


class Claimability:
    def __init__(
        self,
        clock: Callable[[], float],
        clearance: Collection[str],
        window: float = WINDOW_SECONDS,
    ) -> None:
        self._clock = clock
        self._clearance = frozenset(clearance)
        self._window = window
        # (models, capabilities) -> when a request last declared them. Written by request
        # threads, read by the metrics export thread.
        self._seen: dict[tuple[frozenset[str], frozenset[str]], float] = {}
        self._lock = threading.Lock()

    def record(self, models: Iterable[str], capabilities: Iterable[str]) -> None:
        """Called for every lease request that passed authentication, answered or not: an empty
        poll is exactly the evidence that a worker is there."""
        key = (frozenset(models), frozenset(capabilities))
        now = self._clock()
        with self._lock:
            self._seen.pop(key, None)  # re-insert last: dict order is then oldest first
            self._seen[key] = now
            self._prune(now)
            while len(self._seen) > MAX_REMEMBERED:
                del self._seen[next(iter(self._seen))]

    def _prune(self, now: float) -> None:
        for key in [k for k, t in self._seen.items() if now - t > self._window]:
            del self._seen[key]

    def _declared(self, model: str, prompt_version: str) -> bool:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return any(model in m and prompt_version in c for m, c in self._seen)

    def unclaimable(
        self, waiting: Mapping[tuple[str, str, str], int]
    ) -> dict[tuple[str, str], int]:
        """`waiting` maps (model, prompt_version, data_class) to a job count. Returns
        (model, prompt_version) -> how many of those jobs nobody can take; every waiting pair is
        present, zeros included (so a fixed pair reads 0 instead of going stale).

        At most MAX_SERIES entries: the largest counts are kept (ties by name, so the choice is
        stable between exports) and the rest are added into (OTHER, OTHER). Models and prompt
        versions come from admin-enqueued jobs, so this is a guard, not an expected case."""
        counts: dict[tuple[str, str], int] = {}
        for (model, version, data_class), n in waiting.items():
            blocked = data_class not in self._clearance or not self._declared(model, version)
            counts[(model, version)] = counts.get((model, version), 0) + (n if blocked else 0)
        if len(counts) <= MAX_SERIES:
            return counts
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        kept = dict(ranked[: MAX_SERIES - 1])
        kept[(OTHER, OTHER)] = sum(n for _, n in ranked[MAX_SERIES - 1 :])
        return kept
