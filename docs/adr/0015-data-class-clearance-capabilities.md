# 0015. Data class, clearance and capabilities decide which worker may lease a job

**Status:** Accepted (maintainer, 2026-11-25, /grill-with-docs Q1-Q11). Implemented 2026-11-25 (the first step of phase 3).
**Date:** 2026-11-25
**Extends:** [0002](0002-pull-based-broker.md) (footage only to the maintainer's backends); closes the "before
phase 3" gate in [docs/security.md](../security.md).

## Context
The broker leases the oldest job to any caller holding the worker token. Two different questions were hiding behind
one planned "job_class" column:
- **Who may see this input?** A trust boundary: copyrighted footage must never reach a volunteer.
- **Who can run this job?** A capability match: an `echo` worker took `smoke-v1` jobs and wrote different bytes
  under the same result key (ADR 0014's amendment).
They fail differently: a false capability wastes work; a false clearance leaks footage.

## Decision
- Every **Job** states a **Data class**: `footage`, `derived` (text from the game or its footage, including the script
  table and story bible) or `open`. Required at `POST /jobs` (422 if missing). The column is added nullable
  (ADR 0012); NULL is treated as `footage`.
- Each worker token has a **Clearance**, set by broker configuration and never declared by the worker. Today:
  `FLEET_WORKER_CLEARANCE`, required at startup, with no default. Volunteer tokens (phase 10) get `open` only; `derived`
  stays on the maintainer's backends unless the maintainer decides otherwise.
- Workers declare **Capabilities** in the lease request: `{"capabilities": [prompt versions], "models": [...]}`,
  with models read from the model server at startup. A request without them gets a 422.
- The claim stays one atomic statement. It leases only a job whose data class is within the clearance, and whose
  prompt version and model are among the declared ones.
- A job no current worker can run waits in the queue: it's never leased, so it never dead-letters. The phase 9
  progress view will show queued jobs by data class and capability.
- The worker's prompt-version guard (ADR 0014 amendment) stays as a second line of defence.

## Consequences
- Footage can't reach a worker whose token lacks clearance, whatever the worker claims.
- The enforcement is our claim query. A bug there could widen access, so the filter gets its own tests and
  mutation checks.
- Old workers that don't send capabilities fail loudly (422). All workers are the maintainer's until phase 10, so
  they're upgraded together.

## Alternatives considered
- **One `job_class` field for both questions:** it can't express "trusted, but only runs smoke-v1", and it makes
  either the server trust clients about security or the client know the security policy.
- **One queue per data class, with IAM deciding who reads which (e.g. SQS):** the usual managed-service pattern,
  and stronger, because the cloud provider enforces clearance and an application bug can't leak footage. Not
  available in the $0 local setup. It's the translation to use if the broker moves to a managed queue.
- **Worker-declared clearance:** a security boundary the client declares isn't a boundary.
- **Default data class `open` (or `footage`):** `open` leaks footage on the first forgotten field; `footage` hides
  the mistake. Required is explicit.
