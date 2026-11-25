# 0006. Lease lifecycle: token is ownership, lazy expiry, dead letters, idempotent complete

**Status:** Accepted
**Date:** 2026-11-25

## Context
Workers die mid-job, stall, or finish after their lease has run out. Some jobs crash every worker. The broker must
run with no background process, so that it can scale to zero.

## Decision
- **The token is ownership.** Each claim issues a new unguessable token. Heartbeat and complete are conditional
  updates (`WHERE job_id = ? AND lease_token = ? AND state = …`). Zero rows changed means the caller lost the job.
  A lease that expired but that nobody has re-leased can still be renewed.
- **Lazy expiry:** `claim()` treats an expired lease as available and re-leases it with a new token. There is no
  reaper process.
- **Dead letters:** each claim increments `attempts`. Before claiming, `claim()` moves expired jobs with
  `attempts >= max_attempts` (default 3) to `dead`, for a human to look at. That statement hands nothing out, so
  it doesn't need to be atomic with the claim.
- Dead-lettering and claiming both treat a lease as expired at `lease_expires_at <= now`, the same comparison in
  both. A boundary test catches `<` in either one.
- **Idempotent complete:** repeating the same completion (same token and result key) succeeds, so a worker can
  retry after losing the response. A different result key for a finished job is refused.

## Consequences
- At-least-once delivery that matches SQS (claim = ReceiveMessage, heartbeat = ChangeMessageVisibility,
  complete = DeleteMessage, `max_attempts` + `dead` = maxReceiveCount + DLQ).
- Nothing runs while the broker is idle.
- Dead jobs need a way to inspect and requeue them later (an admin endpoint, still open).

## Alternatives considered
- **A background reaper:** it needs a long-running process, which defeats scale-to-zero.
- **A strict expiry check on heartbeat:** it rejects renewals nobody competes with, for no safety gain, since the
  token check already guarantees ownership.
