# Architecture decision records

One short file per decision: what was decided, why, and what it costs. Numbered in order and never
renumbered. A changed decision gets a new ADR that supersedes the old one, and the old one's status is
updated to point to it.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-record-decisions.md) | Record architecture decisions as ADRs | Accepted |
| [0002](0002-pull-based-broker.md) | Pull-based broker; workers never hold cloud keys | Accepted |
| [0003](0003-content-addressed-results.md) | Content-addressed, idempotent results | Accepted |
| [0004](0004-lease-durations-and-clocks.md) | Leases carry durations; worker clock; renew at 2/3 of TTL | Accepted |
| [0005](0005-sqlite-job-store.md) | SQLite job store for the local phases; atomic claim | Superseded by 0020 |
| [0006](0006-lease-lifecycle.md) | Token is ownership; lazy expiry; dead letters; idempotent complete | Accepted |
| [0007](0007-http-contract-and-auth.md) | HTTP status-code contract; interim bearer-token auth | Accepted |
| [0008](0008-short-polling-with-backoff.md) | Short polling with exponential backoff and jitter | Accepted |
| [0009](0009-ci-supply-chain.md) | CI supply chain: SHA-pinned actions, Dependabot cooldown | Accepted |
| [0010](0010-worker-failure-handling.md) | Worker failure handling; invalid output is never stored | Accepted |
| [0011](0011-broker-auth-hardening.md) | Admin and worker tokens; strict inputs; no public API docs | Accepted |
| [0012](0012-context-hash-in-result-key.md) | Context hash in the result key; add-only schema migrations | Accepted |
| [0013](0013-dev-tooling-scripts.md) | Repo scripts for checking, shipping and mutation testing | Accepted |
| [0014](0014-container-images-and-compose.md) | Container images and Compose; model server outside the worker; background heartbeat | Accepted |
| [0015](0015-data-class-clearance-capabilities.md) | Data class, clearance and capabilities decide which worker may lease a job | Accepted (phase 3, step 1) |
| [0016](0016-sqlite-busy-handling.md) | SQLite busy handling: WAL, 503 with Retry-After, client retries | Accepted; SQLite parts superseded by 0020 |
| [0017](0017-terraform-aws-layout-and-state.md) | Terraform for AWS: two stacks, S3 state with native locking, keyless sign-in | Accepted |
| [0018](0018-observability-opentelemetry.md) | Observability with OpenTelemetry: metrics, logs and traces | Accepted (phase 3b) |
| [0019](0019-public-broker-on-cloud-run.md) | Public broker on Cloud Run: app tokens, keyless AWS access, phase 4 order | Accepted (phase 4) |
| [0020](0020-postgres-job-store.md) | PostgreSQL job store: pool, SKIP LOCKED claim, advisory-locked migrations, 503 on connection errors | Accepted (phase 4, step 2) |
| [0021](0021-cloud-run-and-cloud-sql-deployment.md) | Cloud Run and Cloud SQL deployment: sidecar proxy with IAM login, public IP with connectors required, no stored secrets | Accepted (phase 4, step 4) |
| [0022](0022-container-hardening-and-pinned-ci-tools.md) | Read-only, capability-free Compose containers; SHA-pinned hooks; hash-pinned CI tools; enqueue transport guard | Accepted (maintainer, 2026-11-25, Q67) |
| [0023](0023-broker-verifies-result-keys.md) | The broker recomputes the result key and answers 422 to a mismatch; worker maps it to ResultRejected | Accepted (maintainer, 2026-11-25, Q66) |
| [0024](0024-budget-kill-switch.md) | Budget kill switch: at 100% of $20, a private Cloud Run service removes public access to the broker | Accepted (maintainer, 2026-11-25, Q70-Q73) |

Template: Status, Date, Context, Decision, Consequences, Alternatives considered. Keep each one to a page.
