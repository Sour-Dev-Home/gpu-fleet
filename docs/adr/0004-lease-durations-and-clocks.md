# 0004. Leases carry durations; the worker times them; renew at 2/3 of the TTL

**Status:** Accepted
**Date:** 2026-11-25

## Context
The broker and the workers run on different machines whose clocks disagree, sometimes badly, as with notebook VMs.
Wall clocks also jump (NTP, daylight-saving time, laptop sleep).

## Decision
- The broker sends `ttl_seconds`, **never an absolute deadline**. The broker's own deadline never leaves the broker.
- The worker times the lease with `time.monotonic()`. It stamps `received_at` **just before sending** the lease or
  heartbeat request. The broker starts the TTL later than that, so the worker's view of the expiry is never later
  than the broker's.
- The worker renews once `RENEW_AFTER_FRACTION = 2/3` of the TTL has passed. On a 60 s lease that leaves 20 s for
  a slow network or a busy GPU, and the margin scales with short TTLs.
- `needs_renewal` stays true after expiry. The broker decides whether the lease can still be saved.
- Time-dependent logic takes `now` as a parameter, so tests use plain numbers and never `sleep()`.

## Consequences
- The two clocks never need to agree.
- Stamping on reply arrival would overstate the remaining time by up to one round trip. A test with simulated
  latency pins the before-send stamp.
- About 1.5 heartbeats per minute of work.

## Alternatives considered
- **Absolute deadlines:** they break with clock skew.
- **A fixed renewal margin (e.g. 20 s):** it renews constantly on short TTLs.
- **Renew at expiry:** any network delay loses the lease.
