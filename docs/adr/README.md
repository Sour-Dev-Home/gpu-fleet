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
| [0005](0005-sqlite-job-store.md) | SQLite job store for the local phases; atomic claim | Accepted |
| [0006](0006-lease-lifecycle.md) | Token is ownership; lazy expiry; dead letters; idempotent complete | Accepted |
| [0007](0007-http-contract-and-auth.md) | HTTP status-code contract; interim bearer-token auth | Accepted |
| [0008](0008-short-polling-with-backoff.md) | Short polling with exponential backoff and jitter | Proposed |
| [0009](0009-ci-supply-chain.md) | CI supply chain: SHA-pinned actions, Dependabot cooldown | Accepted |

Template: Status, Date, Context, Decision, Consequences, Alternatives considered. Keep each one to a page.
