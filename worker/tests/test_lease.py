from fleet_worker.protocol import Job, Lease, result_key

JOB = Job(
    job_id="j1",
    input_sha256="a" * 64,
    model="qwen3-vl:8b",
    prompt_version="describe-v1",
)


def lease(ttl_seconds: float = 60.0, received_at: float = 1000.0) -> Lease:
    # received_at is an arbitrary monotonic reading: only differences between readings mean anything
    return Lease(job=JOB, lease_token="t", ttl_seconds=ttl_seconds, received_at=received_at)


def test_job_result_key_matches_protocol_function():
    assert JOB.result_key == result_key("a" * 64, "qwen3-vl:8b", "describe-v1")


def test_lease_expires_after_ttl():
    assert not lease().is_expired(now=1059.9)
    assert lease().is_expired(now=1060.0)


def test_fresh_lease_does_not_need_renewal():
    assert not lease().needs_renewal(now=1000.0)


def test_lease_needs_renewal_before_it_expires():
    # whatever the policy, the worker must want to renew while the lease is still valid
    assert lease().needs_renewal(now=1059.0)


def test_lease_needs_renewal_policy():
    # the current policy is to renew when 1/3 of the original TTL remains
    assert not lease(ttl_seconds=60.0).needs_renewal(now=1039.9)
    assert lease(ttl_seconds=60.0).needs_renewal(now=1040.0)


def test_renewal_point_scales_with_short_ttl():
    # a fixed margin (e.g. "20 s before expiry") would pass the 60 s test but renew at once here
    assert not lease(ttl_seconds=6.0).needs_renewal(now=1003.0)
    assert lease(ttl_seconds=6.0).needs_renewal(now=1004.0)


def test_expired_lease_still_asks_for_renewal():
    # the broker, not the worker, decides whether an expired lease can be saved
    assert lease().needs_renewal(now=1060.0)
    assert lease().needs_renewal(now=2000.0)
