import pytest
from fleet_broker.ratelimit import TokenBucket, retry_after


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def test_a_clock_that_steps_back_does_not_lock_workers_out():
    # The broker's clock is wall time; NTP can move it backwards. Negative elapsed time must not
    # subtract tokens, or a 100 s step back would refuse every request for 100 s.
    clock = Clock()
    bucket = TokenBucket(rate=1, burst=2, clock=clock)
    assert bucket.take() == 0
    clock.now -= 100
    assert bucket.take() == 0


def test_the_wait_is_until_the_next_token():
    clock = Clock()
    bucket = TokenBucket(rate=4, burst=1, clock=clock)
    assert bucket.take() == 0
    assert bucket.take() == pytest.approx(0.25)
    assert retry_after(0.25) == "1"  # whole seconds, rounded up
    assert retry_after(2.1) == "3"


@pytest.mark.parametrize(
    ("rate", "burst"),
    [
        (0, 1),
        (-1, 1),
        (1, 0),
        (float("nan"), 1),  # nan <= 0 is False: it must still be refused
        (float("inf"), 1),  # would never run out
        (5e-324, 1),  # the wait would overflow Retry-After
        (1, 10**9),
    ],
)
def test_nonsense_limits_are_refused(rate, burst):
    with pytest.raises(ValueError):
        TokenBucket(rate, burst, Clock())
