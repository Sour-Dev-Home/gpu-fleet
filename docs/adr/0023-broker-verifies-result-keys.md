# 0023. The broker verifies result keys

**Status:** Accepted (maintainer, 2026-11-25, Q66)
**Date:** 2026-11-25
**Builds on:** [0006](0006-lease-lifecycle.md), [0007](0007-http-contract-and-auth.md),
[0012](0012-context-hash-in-result-key.md).

## Context
`/complete` took the worker's `result_key` on trust. A worker holding job A's lease could name job B's result,
and the broker would record A as done with B's content. Results are content-addressed, so the key is a pure
function of fields the broker already stores: nothing needs trusting.

## Decision
- On `/complete` the broker recomputes the key from the job's own row (`input_sha256`, `model`,
  `prompt_version`, `context_sha256`, exactly as the worker does) and refuses any other.
- **422 Unprocessable Content**, not 409. 409 already means "lease lost" to the worker (it moves on, nothing
  to retry); a mismatch is the opposite: the lease is fine, the request is wrong. 5xx and 429 mean "retry
  later" and a retry would send the same key. 422 is also already the API's "well-formed but not acceptable".
  FastAPI also answers 422 for a malformed body, so the refusal is machine-readable:
  `{"detail": "...", "code": "result_key_mismatch"}`. The worker raises `ResultRejected` only for that code;
  any other 422 stays an HTTP error and stops the worker loudly (ADR 0007's table has the row).
- **A mismatch stops the worker.** It is a formula bug, so every further job would fail the same way and a
  drifted worker would burn the queue into dead-letter. `ResultRejected` is a `SystemExit` like
  `UnsupportedPromptVersion`: the loop logs an error naming the job id and re-raises; the lease expires.
  The result object the worker already stored under its wrong key is harmless: content-addressed under a key
  nothing points to. A test shows a worker that always sends a wrong key dead-letters its job after
  `max_attempts`.
- The check is in the store and applies to a leased **and** an already done job, so a retry with the correct
  key still returns 204 and one with a wrong key is refused after the fact. A refused completion changes
  nothing: the job stays leased (or done, unchanged).
- Only the holder of the job's current lease token can get a 422. A stale or unknown token still gets 409, so
  the answer reveals nothing about a job to someone who does not hold it.
- Observability: counter `fleet.results.rejected` (Prometheus `fleet_results_rejected_total`) on the
  "Trouble signs" panel, and a warning log with the job id only (the key is the client's claim).
- **One formula, two copies.** The packages share no code (the worker ships to volunteers, the broker to
  Cloud Run), and a shared package would add a third thing to version and publish for ten lines.
  `broker/src/fleet_broker/addressing.py` duplicates `result_key` from `worker/src/fleet_worker/protocol.py`;
  `scripts/tests/test_result_key_agreement.py` loads both files and fails if they disagree on a table of
  vectors or if the digest of a pinned vector changes (stored results were written under it). The unicode,
  escaped and empty-context vectors are formula guards: enqueue validation keeps such values from reaching
  the broker today, but the two copies must still agree if validation ever loosens.

## Consequences
- A worker with a different formula (an old client after a change to the key) fails every job loudly instead
  of storing results where nobody looks. Change the formula only with a new prompt-version or a coordinated
  release, and update both copies in one PR (the test enforces it).
- The broker still does not check that the result exists in storage or is valid; that is the phase 10
  quarantine work (docs/security.md).
- Old rows: a retried `/complete` on a done row written before this change, with a key that is not the
  formula's, now gets 422. Harmless: production rows always used the formula.
- Cost: one extra indexed SELECT by primary key per completion.

## Alternatives considered
- Shared module or package: rejected for the reason above.
- Compute the key in the broker and ignore the client's: loses the loud failure that finds a drifted worker.
- 409 with a different detail string: workers would have to parse text to tell it from a lost lease.
