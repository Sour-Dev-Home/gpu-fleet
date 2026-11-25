"""The job protocol shared by worker and broker.

A result's storage key is derived from everything that determines its content:
the input's SHA-256, the model, and the prompt version. Two workers that run the
same job (at-least-once delivery) therefore write identical bytes to the same key,
so a duplicate run is a harmless overwrite, and a finished result is never recomputed.
"""

import hashlib
import json
from dataclasses import dataclass


def result_key(input_sha256: str, model: str, prompt_version: str) -> str:
    """Return the content address (64 lowercase hex chars) for a job's result.

    input_sha256: hex SHA-256 of the job's input bytes (e.g. a clip or a transcript).
    model: model identifier, e.g. "qwen3-vl:8b".
    prompt_version: version label of the prompt template, e.g. "describe-v3".
    """

    data = json.dumps([input_sha256, model, prompt_version])
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Job:
    """A unit of work, as the broker describes it. Frozen: a job never changes once issued."""

    job_id: str
    input_sha256: str
    model: str
    prompt_version: str

    @property
    def result_key(self) -> str:
        return result_key(self.input_sha256, self.model, self.prompt_version)


# Renew when 1/3 of the original TTL remains: room for a slow network or a busy GPU.
RENEW_AFTER_FRACTION = 2 / 3


@dataclass(frozen=True)
class Lease:
    """The worker's right to run one job for a limited time.

    The broker sends a duration (ttl_seconds), not a deadline, so the two machines' clocks
    never have to agree. The worker stamps received_at with ITS OWN time.monotonic() when
    the lease arrives; every time comparison below uses that same clock.
    """

    job: Job
    lease_token: str  # proves to the broker that this worker holds the lease (heartbeat, complete)
    ttl_seconds: float
    # time.monotonic() on this worker when the lease was granted or renewed
    received_at: float

    @property
    def expires_at(self) -> float:
        return self.received_at + self.ttl_seconds

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def needs_renewal(self, now: float) -> bool:
        """True when the worker should send a heartbeat now (before the lease can run out).

        `now` is passed in rather than read from the clock, so tests can use plain numbers.
        """
        return now >= self.received_at + self.ttl_seconds * RENEW_AFTER_FRACTION
