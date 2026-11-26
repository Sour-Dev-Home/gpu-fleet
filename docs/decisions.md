# Decisions

The design choices behind the fleet, by theme. Each entry gives the question, the alternatives, the choice and the
reason, with a link to the ADR that holds the detail. Numbers marked "napkin" are back-of-the-envelope estimates.

## How decisions are made here

A decision starts as a numbered question (the Q-numbers below) put to the maintainer in a "grill round": a short
list, each with options, a recommendation and the cost of being wrong. Researched facts are tagged UNVERIFIED until
an official page or a measurement confirms them ([research notes](research/phase-4.md)). The answer is recorded as
an ADR in the same pull request as the code ([ADR 0001](adr/0001-record-decisions.md), index in
[docs/adr](adr/README.md)); a changed decision gets a new ADR that supersedes the old one. Security-relevant changes
get an independent review before they apply, and the dangerous paths are drilled for real (the kill switch has been
fired twice, see [ADR 0024](adr/0024-budget-kill-switch.md)). Behaviour changes need a test that fails without them,
checked by mutation testing.

## Security

- **Who holds cloud keys?** Workers, or nobody? Chosen: nobody. Workers pull jobs over HTTPS and get per-object
  presigned URLs; the broker reaches AWS by federation, never a stored key. A leaked worker token cannot touch cloud
  accounts. [0002](adr/0002-pull-based-broker.md), [0019](adr/0019-public-broker-on-cloud-run.md),
  [0025](adr/0025-presigned-urls-and-aws-federation.md)
- **How does Google Cloud get into AWS?** Stored AWS keys vs web identity federation. Chosen: the broker's Google ID
  token for `AssumeRoleWithWebIdentity`, trust pinned to its service account's unique ID and our own audience, 1 h
  sessions. A stolen token is useless after an hour and to anyone else (Q40, Q78). [0019](adr/0019-public-broker-on-cloud-run.md),
  [0025](adr/0025-presigned-urls-and-aws-federation.md)
- **Which footage may leave the maintainer's machines?** Chosen: every job states a data class; clearance belongs to
  the broker's configuration, capabilities come from the worker, and all three are checked in the one atomic claim.
  Volunteers get `open` only. [0015](adr/0015-data-class-clearance-capabilities.md)
- **One token or two?** Chosen: separate worker and admin tokens, 32+ characters, compared in constant time, strict
  request models, no public API docs; rotation accepts a `*_PREVIOUS` token. A worker token cannot add jobs.
  [0011](adr/0011-broker-auth-hardening.md)
- **Public broker with app tokens, or private behind Google IAM?** Chosen: public with app tokens, since notebooks
  and volunteers cannot easily mint Google ID tokens (Kaggle has no supported route); private deferred to phase 10
  (Q39, Q65). [0019](adr/0019-public-broker-on-cloud-run.md), [0021](adr/0021-cloud-run-and-cloud-sql-deployment.md)
- **What stops a flood from spending money?** Chosen: three layers. An instance cap bounds compute; a kill switch
  removes public access at 100% of $20 gross spend (hours of lag); a 4xx-rate alert trips the same switch within
  minutes. The switch only removes access, the safe direction, and `terraform apply` restores it. It was fired for
  real: public 403 about 2 minutes after the billing notification. [0024](adr/0024-budget-kill-switch.md),
  [0028](adr/0028-phase4-security-review.md)
- **Could a replayed or forged alert undo a manual restore?** Chosen: a strict parser per message shape, only open
  incidents of the named policy, and a one-hour replay window. Undatable messages are ignored on purpose.
  [0028](adr/0028-phase4-security-review.md)
- **Can an over-broad identity in the AWS account read the data?** Chosen: an explicit bucket-policy Deny of
  `s3:*Object*` on `inputs/*` and `results/*` for everyone but the broker role and the admin principal; bucket
  management stays reachable so Terraform cannot lock itself out. Rejected: `NotPrincipal`, which misbehaves with role
  sessions (Q94). [0028](adr/0028-phase4-security-review.md)
- **Can a worker complete job A with job B's result?** Chosen: the broker recomputes the result key and answers 422 to a
  mismatch, so the worker stops instead of retrying a formula bug. [0023](adr/0023-broker-verifies-result-keys.md)
- **Can a read of the database take over leases?** Chosen: the store holds the SHA-256 of the lease token, and the
  token (128 random bits) is ownership. [0006](adr/0006-lease-lifecycle.md)
- **Are containers and CI tools trusted?** Chosen: read-only, capability-free containers; hooks pinned to commit SHAs;
  CI tools installed by hash; actions pinned to SHAs; 7-day Dependabot cooldown; `pip-audit` on every lockfile.
  [0009](adr/0009-ci-supply-chain.md), [0022](adr/0022-container-hardening-and-pinned-ci-tools.md),
  [0028](adr/0028-phase4-security-review.md)
- **How do secrets stay out of state and git?** Chosen: login-based AWS access (no access keys), a local vault, sops
  plus age for anything committed, and Terraform containers whose secret versions the maintainer adds by hand.
  [0017](adr/0017-terraform-aws-layout-and-state.md), [0027](adr/0027-telemetry-destination.md)

## Cost

- **Where does compute run?** Chosen: $0 compute (local GPU, free notebooks, Google Cloud credits); AWS only
  coordinates (S3). Cloud capacity scales from the backlog, so no idle cloud workers. [0002](adr/0002-pull-based-broker.md),
  [0008](adr/0008-short-polling-with-backoff.md)
- **Request-based or always-on Cloud Run billing?** Chosen: request-based, scale to zero. Instance-based 24x7 is about
  $44 a month (napkin); idle workers back off to 60 s, so 10 of them make about 0.43M requests a month against 2M
  free (Q65). [0021](adr/0021-cloud-run-and-cloud-sql-deployment.md)
- **Which database tier?** Chosen: Cloud SQL `db-f1-micro` with 10 GB, about $9 a month in all (read from the console
  form), over `db-g1-small` at about $29. It is the only fixed cost, and the kill switch cannot stop it.
  [0021](adr/0021-cloud-run-and-cloud-sql-deployment.md), [0024](adr/0024-budget-kill-switch.md)
- **Public IP or private IP for the database?** Chosen: public IP with connectors required, TLS only and no
  authorized networks; private IP (Direct VPC egress, private services access) rejected for now as more to learn and pay
  for. [0021](adr/0021-cloud-run-and-cloud-sql-deployment.md)
- **What counts toward the $20 budget?** Chosen: gross of credits. Net of the roughly $296 credits the figure would
  stay near $0 and never fire. Normal running is about $10-11. [0024](adr/0024-budget-kill-switch.md)
- **Cloud Armor or an app-level limit?** Chosen: a token bucket per role and a body limit in the app. Cloud Armor
  needs a load balancer, about $18 a month (Q46). [0019](adr/0019-public-broker-on-cloud-run.md)
- **A Cloud Function or our own service for the kill switch?** Chosen: our own container (about 100 lines); a function
  drags in Cloud Build, Artifact Registry builds and Eventarc. [0024](adr/0024-budget-kill-switch.md)
- **Always-on CPU for telemetry export?** Rejected: about 5M vCPU-seconds a month against 180k free. Chosen: a
  Scheduler call each minute (43,200 requests, about 26k vCPU-seconds napkin, inside the free tier). Secret Manager
  adds about $0.06 a month (7 versions against 6 free). [0027](adr/0027-telemetry-destination.md)

## Data

- **What do we store, and under which key?** Chosen: results are content-addressed by input hash, model, prompt
  version and (when present) context hash, in JSON so field boundaries are unambiguous. The same work is never done
  twice, and a result is stored before the job is completed. [0003](adr/0003-content-addressed-results.md),
  [0012](adr/0012-context-hash-in-result-key.md)
- **SQLite or PostgreSQL?** Chosen: SQLite with an atomic claim for the local phases, then PostgreSQL (psycopg pool
  capped at 4, `SKIP LOCKED` claim, advisory-locked migrations, 503 on connection errors) once the broker was
  networked. [0005](adr/0005-sqlite-job-store.md), [0016](adr/0016-sqlite-busy-handling.md), [0020](adr/0020-postgres-job-store.md)
- **How are schema changes made?** Chosen: add-only nullable columns (expand before contract), so old rows and old
  keys stay valid. [0012](adr/0012-context-hash-in-result-key.md)
- **How do bytes move?** Chosen: one bucket, `inputs/<hash>` and `results/<key>`; the input URL rides in `/lease`
  (10 min), the result URL comes from `/upload-url` (5 min, lease token required), 10 MB cap. S3 was measured
  enforcing the signed length, SHA-256 and no-overwrite (403, 400, 412); `/complete` HEADs the object before accepting.
  [0025](adr/0025-presigned-urls-and-aws-federation.md)
- **How does Terraform state stay safe?** Chosen: two AWS stacks (bootstrap and fleet), S3 state with native locking
  (no DynamoDB), versioned with 90-day expiry, and the Google stack's state in the same bucket.
  [0017](adr/0017-terraform-aws-layout-and-state.md), [0019](adr/0019-public-broker-on-cloud-run.md)

## Observability

- **Which telemetry standard?** Chosen: OpenTelemetry for metrics, logs and traces, with a local Grafana LGTM stack for
  development. The code instruments once and the destination is configuration. No job IDs on metric labels, and no
  tokens or content in logs. [0018](adr/0018-observability-opentelemetry.md)
- **How do we see a job nobody can run?** Chosen: a `fleet.jobs.unclaimable` gauge (jobs whose model and prompt
  version no recent lease request declared) with an alert, a worker start line stating what it can run, and a cancel
  endpoint. This came from a real silent failure (Q84-Q86). [0026](adr/0026-telemetry-signals.md)
- **How is a worker identified?** Chosen: a random install ID stored in the user profile, never derived from hardware;
  friendly names live only in dashboard value mappings; volunteers get per-job numbers only (Q69, Q87).
  [0026](adr/0026-telemetry-signals.md)
- **Where does telemetry go?** Chosen: Grafana Cloud Free (10k series, 14-day retention, no card) over OTLP, one
  write-only token per source with 90-day expiry. A leak adds noise to our quota and nothing else. Rejected: reading
  Cloud Monitoring from Grafana (needs a stored key); platform metrics stay in the Google console (Q88-Q92).
  [0027](adr/0027-telemetry-destination.md)
- **How do the PC's metrics get there?** Chosen: Grafana Alloy as a Windows service with the embedded Windows exporter
  (CPU, memory, disk, network, GPU) and its own token in a protected file (Q91). [0027](adr/0027-telemetry-destination.md)

## Operations

- **Push or pull for work?** Chosen: pull. Workers poll `/lease` with exponential backoff and full jitter (cap about
  60 s); a busy worker asks again at once. [0002](adr/0002-pull-based-broker.md), [0008](adr/0008-short-polling-with-backoff.md)
- **How long is a lease, and whose clock counts?** Chosen: the broker sends a duration, never a deadline; the worker
  times it with a monotonic clock and renews at two thirds of the TTL. [0004](adr/0004-lease-durations-and-clocks.md)
- **What happens to a job whose worker dies?** Chosen: lazy expiry (no reaper), an attempt counter, dead letters after
  `max_attempts`, and an idempotent complete. [0006](adr/0006-lease-lifecycle.md)
- **How should a worker react to each failure?** Chosen: status codes encode the reaction (409 lost lease, 422
  rejected result, 401 stop loudly, 5xx retry); invalid output is never stored. [0007](adr/0007-http-contract-and-auth.md),
  [0010](adr/0010-worker-failure-handling.md)
- **Where does the broker run, and how does it log in to its database?** Chosen: Cloud Run with a Cloud SQL Auth Proxy
  sidecar and IAM login, no password (Q56). Kubernetes is a backend, not the control layer.
  [0002](adr/0002-pull-based-broker.md), [0021](adr/0021-cloud-run-and-cloud-sql-deployment.md)
- **How are images built and run?** Chosen: a worker image without a model runtime (the model server is separate),
  digest-pinned non-root bases, Compose for local runs. [0014](adr/0014-container-images-and-compose.md)
- **How is service restored after a kill-switch trip?** Chosen: a reviewed `terraform apply`; the plan showing the
  public binding "to add" is the signal. A `check` warns at plan time. Drilled: 404 again about 5 minutes after the
  budget drill's 403, with a flood-path drill showing about 90 s of IAM propagation. [0024](adr/0024-budget-kill-switch.md)

## Tooling

- **How are decisions recorded?** Chosen: ADRs in the same PR as the change, Proposed until approved.
  [0001](adr/0001-record-decisions.md)
- **What is "good enough to commit"?** Chosen: one command, `scripts/check.sh` (pre-commit plus every package's tests),
  which CI runs too so they cannot drift; `scripts/ship-pr.sh` ships; `scripts/mutate.py` proves tests catch bugs.
  [0013](adr/0013-dev-tooling-scripts.md)
- **How is the supply chain pinned?** See Security: SHA-pinned actions and hooks, hash-pinned CI tools, locked installs,
  Dependabot with a cooldown, and the oldest supported Python in CI. [0009](adr/0009-ci-supply-chain.md),
  [0022](adr/0022-container-hardening-and-pinned-ci-tools.md)
