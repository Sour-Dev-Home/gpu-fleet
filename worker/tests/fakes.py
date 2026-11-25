"""In-memory stand-ins for the broker, the result store and the clock."""

from dataclasses import replace

from fleet_worker.loop import LeaseLost
from fleet_worker.protocol import Lease


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.puts = 0

    def exists(self, key: str) -> bool:
        return key in self.data

    def put(self, key: str, data: bytes) -> None:
        self.puts += 1
        self.data[key] = data


class FakeBroker:
    def __init__(self, leases: list[Lease], store: FakeStore, clock: FakeClock) -> None:
        self.leases = list(leases)
        self.store = store
        self.clock = clock
        self.heartbeats: list[Lease] = []
        self.completed: list[tuple[Lease, str]] = []
        self.refuse_heartbeats = False

    def lease(self) -> Lease | None:
        return self.leases.pop(0) if self.leases else None

    def heartbeat(self, lease: Lease) -> Lease:
        self.heartbeats.append(lease)
        if self.refuse_heartbeats:
            raise LeaseLost(lease.job.job_id)
        return replace(lease, received_at=self.clock(), lease_token=lease.lease_token + "+")

    def complete(self, lease: Lease, result_key: str) -> None:
        # A real broker can't see S3 this directly, but the rule is the same: never mark
        # a job done whose result isn't stored yet.
        assert self.store.exists(result_key), "completed before the result was stored"
        self.completed.append((lease, result_key))
