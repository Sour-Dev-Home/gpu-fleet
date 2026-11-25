# 0010. Worker failure handling: retry transient, stop on fatal, never store invalid output

**Status:** Accepted (maintainer, 2026-11-25). It implements [0007](0007-http-contract-and-auth.md) and
[0008](0008-short-polling-with-backoff.md) on the worker; the output-validation rule follows the Metaphor #1013
lesson "code owns structure".
**Date:** 2026-11-25

## Context
A long-running worker meets several kinds of failure, and each needs a different reaction. The local queue run
for Metaphor #1013 also showed models returning empty or malformed output. Storing that output would give it a
content address, and the cache would then serve it forever ([0003](0003-content-addressed-results.md)).

## Decision
`Worker.run()` loops until told to stop:

| Failure | Raised as | Reaction |
|---|---|---|
| Queue empty (204) | `run_once()` returns None | Back off with jitter |
| Network error, 5xx | `TransientError` (mapped in `HttpBroker._post`) | Back off with jitter |
| Lost the job (409) | `LeaseLost` | Log, reset the backoff, take the next job right away |
| Output fails validation | `InvalidResult` | Don't store and don't complete. Let the lease expire, so the job is retried and the failure counts toward `max_attempts` ([0006](0006-lease-lifecycle.md)). Back off so a broken runner can't spin |
| Anything else (401, bugs) | propagates | The worker stops loudly. Misconfiguration must not retry forever |

- Validation runs **before** `store.put`. The default rejects empty output. Real runners pass a stricter
  validator (schema, IDs and timestamps from code, length limits).
- A successful job resets the backoff.

## Consequences
- Bad output can never become a cached result.
- A job that keeps producing invalid output ends up dead-lettered after `max_attempts`, not looping.
- An invalid result costs up to one TTL (60 s) before the retry. That's negligible for batch work. An explicit
  `/release` endpoint could make it immediate if that ever matters.

## Alternatives considered
- **Retry on every error:** a wrong token would hammer the broker forever without anyone noticing.
- **Store invalid output with a flag:** it pollutes the content-addressed store, and every reader has to check the flag.
