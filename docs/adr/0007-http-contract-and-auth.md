# 0007. HTTP status-code contract; interim bearer-token auth

**Status:** Accepted for local use. Auth must be reviewed (security review, maintainer sign-off) before the broker
is reachable from the internet in phase 4.
**Date:** 2026-11-25
**Amended by** [0023](0023-broker-verifies-result-keys.md) (2026-11-25): the 422 row below.

## Context
The worker's reaction depends on *why* a request failed. One shared token is enough while the broker runs only
locally. Phase 10 adds volunteers.

## Decision
Status codes are chosen by what the caller should do:

| Code | Meaning | Worker reaction |
|---|---|---|
| 200 / 201 | Done | Continue |
| 204 from `/lease` | No work | Back off ([0008](0008-short-polling-with-backoff.md)) |
| 409 from `/heartbeat`, `/complete` | Caller no longer owns the job | Raise `LeaseLost`, don't retry |
| 422 from `/complete` with `"code": "result_key_mismatch"` | The result key is not the job's content address (0023) | Raise `ResultRejected`: stop the worker, don't retry. Any other 422 (a malformed body) is an HTTP error: stop loudly |
| 401 | Bad or missing token | Configuration error: stop loudly, never treat as lease lost |
| 5xx, network errors | Broker trouble | Retry with backoff |

- Auth: `Authorization: Bearer <FLEET_WORKER_TOKEN>`, compared with `hmac.compare_digest` (constant time).
- The token comes from the `fleet` vault through `scripts/with-secrets.ps1` ([docs/secrets.md](../secrets.md)).
  Without the token, no app is created.
- FastAPI with Pydantic request and response models. Tests use `TestClient` in-process, with no ports.

## Consequences
- The worker client can be thin: 204 means None, 409 means LeaseLost, anything else unexpected raises.
- One shared token can't tell workers apart or revoke one worker. Phase 10 replaces it with per-worker tokens,
  as a new ADR.

## Alternatives considered
- **gRPC:** heavier tooling, and notebooks and browsers handle plain HTTPS/JSON more easily.
- **Error details in a 200 body:** it hides failures from HTTP tooling and from retry logic.
