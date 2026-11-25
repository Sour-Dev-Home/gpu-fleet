# 0003. Content-addressed, idempotent results

**Status:** Accepted
**Date:** 2026-11-25 (principle from 2026-11-24)

## Context
Delivery is at-least-once ([0006](0006-lease-lifecycle.md)): a slow worker can lose its lease and still finish,
so a job sometimes runs twice. Workers also crash between steps.

## Decision
- A result is stored under `result_key(input_sha256, model, prompt_version)`: the SHA-256 of
  `json.dumps([input_sha256, model, prompt_version])`, encoded as UTF-8.
- JSON encoding makes field boundaries unambiguous. Plain concatenation would map `("ab","c")` and `("a","bc")`
  to the same key.
- Keys are case-sensitive. The broker canonicalizes model names before building jobs.
- The worker loop checks the store first, then runs, **stores, then completes**. Never complete before storing.

## Consequences
- A duplicate run overwrites identical bytes at the same key, so it's harmless.
- A crash after storing is repaired for free: the next worker finds the result and only completes.
- Results double as a cache, so the same work under a new job id is never recomputed.
- Changing the key scheme makes every old result look missing. A new scheme needs a version prefix and an ADR.
- Jobs whose output depends on more than these three fields (for example Metaphor pass 2, which depends on the
  story bible) must add that context to the key, or the cache serves stale results. See
  [0012](0012-context-hash-in-result-key.md).

## Alternatives considered
- **Random result ids plus a "done" flag:** duplicates create orphaned copies, and there's no free cache.
- **Exactly-once delivery:** very costly in a distributed system and unnecessary once duplicates are harmless.
