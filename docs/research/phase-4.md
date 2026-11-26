# Phase 4 research (2026-11-25)

Gathered by the `researcher` agent, 2026-11-25. Most Google pages show no date through the fetcher, so every row
is dated by the check date. UNVERIFIED: no official page fetched states it. "(snippet)": the official URL came back
only as a search-result excerpt because the page fetch was truncated. Re-check the live page before spending money.

| Topic | Finding (quoted where possible) | Source | Notes |
|---|---|---|---|
| Cloud Run free tier, request-based | "2 million requests per month." "360,000 GB-seconds of memory, 180,000 vCPU-seconds of compute time." "1 GB of outbound data transfer from North America per month." | https://docs.cloud.google.com/free/docs/free-cloud-features | Eligible regions UNVERIFIED; a pricing-page snippet says it's based on us-central1 (Tier 1) pricing. |
| Cloud Run free tier, instance-based | 240,000 vCPU-seconds and 450,000 GiB-seconds free (snippet) | https://cloud.google.com/run/pricing | UNVERIFIED. |
| Tier 1 regions | us-central1, us-east1, us-east4, us-east5, us-south1, us-west1, europe-west1, europe-west4 and others | https://docs.cloud.google.com/run/docs/locations | Tiers only; not which get the free tier. |
| Idle cost at min-instances 0 | Minimum instances default to `0`. Request-based billing: instances "are only charged when they process requests, when they start, and when they shut down." | https://docs.cloud.google.com/run/docs/configuring/min-instances ; https://docs.cloud.google.com/run/docs/configuring/billing-settings | $0 idle, at the price of a cold start on the first request. |
| Max request body | "32 MiB" with HTTP/1; no limit with an HTTP/2 server. Default timeout 5 min, max 60. | https://docs.cloud.google.com/run/quotas | The broker passes URLs, never footage. |
| CPU throttling and background threads | "CPU is only allocated during request processing." "Running background threads with request-based billing enabled can result in unexpected behavior." | https://docs.cloud.google.com/run/docs/configuring/billing-settings ; https://docs.cloud.google.com/run/docs/tips/general | OTel batch exporters starve between requests: instance-based billing, in-request flush, or delayed export. |
| Instance-based billing, 24x7 | About $0.000018/vCPU-s and $0.000002/GiB-s (Tier 1) (snippet) | https://cloud.google.com/run/pricing | UNVERIFIED rates. 1 vCPU, 512 MiB, 24x7 minus free: about $44/month. Not $0. |
| Per-client rate limiting | Not found in Cloud Run docs | https://docs.cloud.google.com/run/docs/triggering/https-request | Only max-instances and concurrency (UNVERIFIED as complete). Rate-limit in the app. |
| Firestore free quota | "1 GiB of storage per project." "50,000 reads, 20,000 writes, and 20,000 deletes per day per project." | https://docs.cloud.google.com/free/docs/free-cloud-features ; https://firebase.google.com/docs/firestore/quotas | Default database only: UNVERIFIED. Polling reads count against 50k/day. |
| Firestore transactions | Reads before writes; "Firestore runs the entire transaction again" on contention; Standard edition "transactions place locks on the documents they read." | https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions ; https://docs.cloud.google.com/firestore/native/docs/transaction-data-contention | A query inside a transaction: UNVERIFIED. |
| Firestore composite index | "an `ORDER BY a ASC, b ASC` query requires a composite index" | https://docs.cloud.google.com/firestore/native/docs/query-data/multiple-range-fields | Equality filters plus orderBy on another field: UNVERIFIED verbatim. |
| Cloud SQL for PostgreSQL | No always-free tier found; new customers "$300 in free credits". Third parties: db-f1-micro about $8/month. | https://cloud.google.com/sql/pricing ; https://www.bytebase.com/dbcost/cloudsql-pricing/ | Price UNVERIFIED. Credits only. |
| Neon free plan | "100 CU-hours per project each month"; "1 GB of Postgres storage per project"; "Compute suspends after 5 minutes of inactivity, and you can't turn this off on Free"; restart "typically within a few hundred milliseconds". | https://neon.com/pricing (page updated 2026-11-04) ; https://neon.com/docs/introduction/scale-to-zero | Constant polling keeps it awake; 24x7 at 0.25 CU = 180 CU-h, over 100 (0.25 CU minimum UNVERIFIED). |
| Supabase free plan | "500 MB database size per project"; "Limit of 2 active projects."; "Free projects are paused after 1 week of inactivity." | https://supabase.com/pricing | Cold start after a pause: not found. |
| `SKIP LOCKED` | "can be used to avoid lock contention with multiple consumers accessing a queue-like table." | https://www.postgresql.org/docs/current/sql-select.html | Core Postgres; vendor statements UNVERIFIED. |
| DynamoDB free tier | "25 GB of data storage"; credits "up to $200 USD ... for up to six months". | https://aws.amazon.com/dynamodb/pricing/on-demand/ | Applies to Free-plan accounts: UNVERIFIED. The AWS account closes around late Jan 2027 unless upgraded. |
| SQLite on Cloud Storage FUSE | "does not support file locking"; "shouldn't be used as the backend for storing a database"; "the last write wins and all previous writes are lost." | https://docs.cloud.google.com/storage/docs/cloud-storage-fuse/overview ; https://docs.cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts | Unsafe even at max-instances 1 (rollouts overlap instances). |
| SQLite on an in-memory volume | "the data in the volume is permanently deleted" when the instance stops; one volume per instance. | https://docs.cloud.google.com/run/docs/configuring/services/in-memory-volume-mounts | A cache at most. |
| Artifact Registry | "0.5 GB of storage per month" free; $0.10/GB above; same-location transfer free (snippet). | https://docs.cloud.google.com/free/docs/free-cloud-features ; https://cloud.google.com/artifact-registry/pricing | One 230 MB image fits; delete old ones. |
| Secret Manager | "6 active secret versions per month." "10,000 access operations per month." | https://docs.cloud.google.com/free/docs/free-cloud-features | |
| Cloud Armor | $5/policy/month, $1/rule/month, $0.75 per million requests (snippet); attaches to a load balancer backend | https://cloud.google.com/armor/pricing ; https://docs.cloud.google.com/armor/docs/integrating-cloud-armor | UNVERIFIED prices. Needs an external Application Load Balancer. |
| Load balancer | "First 5 forwarding rules $0.025 / 1 hour" (snippet): about $18/month | https://cloud.google.com/load-balancing/pricing | Over budget unless credits pay; the run.app URL bypasses it unless ingress is restricted. |
| API Gateway | $0 for 0-2M calls/month, then $3.00 per million (snippet) | https://cloud.google.com/api-gateway/pricing | UNVERIFIED. |
| Cloud Logging | "First 50 GiB of log data per project per month." | https://docs.cloud.google.com/free/docs/free-cloud-features | $0.50/GiB beyond (snippet). |
| Cloud Monitoring | "All non-chargeable Google Cloud metrics"; custom metrics first 150 MiB (snippet) | https://cloud.google.com/monitoring/pricing | 150 MiB UNVERIFIED. |
| Cloud Trace | 2.5 million spans per billing account per month free (snippet) | https://cloud.google.com/products/observability/pricing | UNVERIFIED. |
| OTLP from Cloud Run | Google's documented route: "a Cloud Run application with a sidecar collector", config in Secret Manager, app sends to localhost. Direct export to `telemetry.googleapis.com` `/v1/traces` exists. | https://docs.cloud.google.com/stackdriver/docs/instrumentation/opentelemetry-collector-cloud-run ; https://docs.cloud.google.com/stackdriver/docs/instrumentation/choose-approach | Which signals the direct endpoint takes: conflicting pages, UNVERIFIED. |
| Developer Program credits | AI Pro: "$10 USD per month in GenAI and Google Cloud developer credits"; credits "expire one year after the date they are granted"; standalone Premium "Renewals closed on September 18, 2026." | https://developers.google.com/profile/help/benefits | |
| $300 free trial | "valid for 90 days"; no billing during the trial; the trial account auto-closes unless upgraded. | https://docs.cloud.google.com/free/docs/free-cloud-features | The maintainer's account is already upgraded (paid). |
| Budgets don't cap | "an alerts-only budget doesn't automatically cap ... usage or spending." | https://docs.cloud.google.com/billing/docs/how-to/budgets | |
| Budget cap pattern | Pub/Sub + a Cloud Run function that disables billing; "terminates all services in the project"; "Resources might be irretrievably deleted." | https://docs.cloud.google.com/billing/docs/how-to/disable-billing-with-notifications | Last resort only. |
| Cloud Run ID token | `.../instance/service-accounts/default/identity?audience=[AUDIENCE]` with `Metadata-Flavor: Google`; a configured custom audience is allowed. | https://docs.cloud.google.com/run/docs/authenticating/service-to-service | |
| Token claims | `aud` "can be freely chosen by the token requester"; `azp` and `sub` are the service account's unique ID. | https://docs.cloud.google.com/docs/authentication/token-types | |
| AWS condition keys | `accounts.google.com:aud` comes from `azp` when set; then the token's `aud` is `accounts.google.com:oaud`. | https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_iam-condition-keys.html | Trust policy: pin `:sub` to the SA's unique ID and `:oaud` to the chosen audience. Not tested end to end. |

## Napkin math
- The researcher assumed 10 workers polling every 5 s: about 5.2M requests/month, over the free 2M. But idle
  workers back off to a 60 s cap (ADR 0008): 10 idle workers make about 0.43M requests/month. Busy workers send a
  lease, a heartbeat every 40 s and a completion per job, so a 5,000-job run adds about 15-30k.
- Instance-based billing 24x7 is about $44/month; $0 needs request-based billing and min-instances 0.

## Not checked
- The Cloud Run pricing page itself (truncated): free-tier regions, instance-based rates.
- Cloud SQL's official shared-core price; Cloud Armor, API Gateway and load-balancer prices beyond snippets.
- Queries inside Firestore transactions; whether the free quota covers only the default database.
- DynamoDB conditional writes; its 25 GB free storage on Free-plan accounts.
- Whether a collector sidecar's own batching also stalls under request-based billing.

## Console check (maintainer, step 3, 2026-11-25)
Read from the Cloud SQL create form (Enterprise, PostgreSQL 18, us-central1, single zone; not created):
- Shared core, 1 vCPU, 0.614 GB (`db-f1-micro`): $0.01/hour, about $7.30/month; 10 GB storage about
  $1.70/month. About $9/month in all, which confirms the third-party estimate above. Chosen for step 4.
- Shared core, 1 vCPU, 1.7 GB (`db-g1-small`): $0.04/hour, about $29/month.
- The console now defaults to PostgreSQL 18, so local, CI and Cloud SQL all use 18 (ADR 0020).
- The billing budget applies to all projects. Budgets alert; they don't cap spending.

## Step 4 research (2026-11-25)
| Topic | Finding | Source | Notes |
|---|---|---|---|
| max_connections default | by memory: "tiny (~0.5) 25", "small (~1.7) 50" | https://docs.cloud.google.com/sql/docs/postgres/flags | f1-micro = 25 is inferred from its 0.6 GB (UNVERIFIED mapping). A flag; changing it restarts the instance. |
| IAM database auth | flag `cloudsql.iam_authentication` = `on`; roles `roles/cloudsql.instanceUser` (login) and `roles/cloudsql.client` (connect) | https://docs.cloud.google.com/sql/docs/postgres/iam-logins | The IAM user is the service account email without `.gserviceaccount.com`, type `CLOUD_IAM_SERVICE_ACCOUNT`. It starts with no privileges: GRANT as `postgres`. |
| Token lifetime | OAuth tokens "valid for only one hour"; connectors refresh them for pooled connections | https://docs.cloud.google.com/sql/docs/postgres/authentication | Automatic IAM auth needs a connector or the Auth Proxy. |
| Auth Proxy | "the recommended method for connecting to a Cloud SQL instance" | https://docs.cloud.google.com/sql/docs/postgres/connect-auth-proxy | Built-in Cloud Run socket with IAM auth: UNVERIFIED. |
| Python connector | drivers: pymysql, pg8000, psycopg, asyncpg, pytds | https://github.com/GoogleCloudPlatform/cloud-sql-python-connector | psycopg major version not stated: UNVERIFIED. |
| 429 billing | 429 = "no available instance"; billing of these not stated | https://docs.cloud.google.com/run/docs/troubleshooting | UNVERIFIED; check the pricing page footnotes. |
| Private IP | Direct VPC egress: no connector, a /26 subnet, scales to zero. A VPC connector keeps at least 2 always-on instances | https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc ; https://docs.cloud.google.com/vpc/docs/serverless-vpc-access | Private IP also needs private services access; its cost not found. |
| Stopping the instance | "suspends instance charges"; storage and IP charges continue | https://docs.cloud.google.com/sql/docs/postgres/start-stop-restart-instance | |
| Terraform | google provider v8.12.0 (17 Nov); budgets import as `billingAccounts/{id}/budgets/{name}`; Cloud Run v2 `deletion_protection` defaults to true | https://github.com/hashicorp/terraform-provider-google/releases | Argument names from the provider docs on GitHub `main`. |

Decided for step 4 (maintainer, 2026-11-25; goes into step 4's ADR): Q56, Cloud SQL login through the Cloud SQL
Auth Proxy as a Cloud Run sidecar with `--auto-iam-authn`, no password. Still open: public IP with no authorized
networks, or private IP.

## Steps 4-7: research notes and decisions (2026-11-25 to 2026-11-26)
Same rule as above: UNVERIFIED means no fetched official page states it. Decisions are the maintainer's; the ADRs
hold the reasoning: [0021](../adr/0021-cloud-run-and-cloud-sql-deployment.md),
[0024](../adr/0024-budget-kill-switch.md), [0025](../adr/0025-presigned-urls-and-aws-federation.md),
[0026](../adr/0026-telemetry-signals.md), [0027](../adr/0027-telemetry-destination.md),
[0028](../adr/0028-phase4-security-review.md).

### Cloud SQL login and the proxy (step 4; Q56)
- Cloud SQL Auth Proxy as a Cloud Run sidecar with `--auto-iam-authn`: IAM database auth, no password. The broker
  connects to 127.0.0.1:5432 as its service account's IAM user; same `psycopg_pool` code, different
  `FLEET_DATABASE_URL`. IAM user name = the service account email without `.gserviceaccount.com`, type
  `CLOUD_IAM_SERVICE_ACCOUNT`; roles `cloudsql.instanceUser` (login) and `cloudsql.client` (connect). A new IAM user
  "has no privileges": grants are made as `postgres`.
- Proxy v2 flags: `--auto-iam-authn`, `--lazy-refresh`, `--health-check` (`/startup` returns 200 when ready). The
  ingress container needs `depends_on` the proxy and the proxy needs a startup probe, or the order is not enforced.
  Pin the image by digest.
- `connector_enforcement = REQUIRED` rejects direct connections (the proxy counts as a connector); it also blocks
  authorized networks and read replicas. `ssl_mode` ENCRYPTED_ONLY fits. Maintenance window hour is UTC.
- UNVERIFIED: restart on changing the IAM flag, `max_connections` for f1-micro (25 is inferred from 0.6 GB),
  table rights for an IAM service account on PostgreSQL 15+ (plan: GRANT the role to `postgres`, then
  `ALTER DATABASE ... OWNER`; fallback `GRANT ALL ON SCHEMA public`), the public IPv4 charge while running.
- Concurrency default 80 (max 1000); request timeout default 300 s.

### Private Cloud Run, rejected for now (Q65)
- ID tokens: users get one with `gcloud auth print-identity-token` (Google calls it dev-only); `google-auth`
  refuses user credentials; Colab is exactly that case; GitHub Actions works through workload identity and a service
  account; no supported route found for Kaggle. Tokens last 1 hour.
- Whether IAM-rejected requests are billed: UNVERIFIED (sources disagree). 429 billing: not documented.
- Cloudflare Workers free plan: 100k requests/day, then Error 1027 (no charges); one rate-limit rule, 10 s window,
  IP only.
- Outcome: private Cloud Run deferred to phase 10 (Kaggle unsolved, billing claim unverified); the broker stays
  public with app tokens, an instance cap, and the kill switch (ADR 0021, 0024).

### Federation and presigned URLs (step 5; Q74-Q80)
- `accounts.google.com` is built in to AWS: no `aws_iam_openid_connect_provider`; the trust principal is
  `{"Federated": "accounts.google.com"}`. Condition keys: `:aud` maps to `azp` (else `aud`), `:oaud` to `aud`,
  `:sub` to `sub`. A Google metadata token has `sub` = `azp` = the service account's numeric unique ID, `aud` = the
  requested audience, one hour. Pin `:sub` to the unique ID and `:oaud` to our own audience string, checked against a
  real token's claims.
- boto3 can do `AssumeRoleWithWebIdentity` from a token file; whether it re-reads the file on refresh is UNVERIFIED,
  so the broker uses a small refreshable provider that fetches a fresh token. `DurationSeconds` is 900 to the role
  maximum; a presigned URL dies with its role session even if its own expiry is later.
- Presigned PUT with a signed `ChecksumSHA256` rejects a mismatched body (BadDigest). Signed length, checksum and
  `If-None-Match: *` were UNVERIFIED until measured on the real bucket (403 / 400 / 412; `scripts/probe-presign.py`).
  Without `s3:ListBucket` a missing key answers 403, not 404.
- Decided (Q74-Q80): one results bucket with `inputs/<sha256>` and `results/<result key>`; the input GET URL rides in
  `/lease` (10 min), the result PUT URL comes from `POST /upload-url` (5 min, lease token required); the worker
  declares size and SHA-256, 10 MB cap, broker HEADs before `/complete`; URLs for every data class but only to the
  lease holder for its own keys; local mode uses a directory store unless `FLEET_RESULTS_BUCKET` is set; a console
  look-and-cancel walkthrough first.

### Telemetry research (step 6; Q68-Q69, Q84-Q92)
- Device metrics: `amd-smi` is Linux only (WSL2 experimental) and AMD's device-metrics-exporter has no confirmed
  Windows build. `windows_exporter` (MIT) has a `gpu` collector, off by default (adapter memory, temperature, power,
  engine time); sensor support on this GPU generation is UNVERIFIED. Alloy embeds `windows_exporter` 0.31.3. OTel
  `hw.gpu.*` conventions are Development stability. Ollama has no confirmed `/metrics`; `/api/ps` gives
  `size_vram` and `/api/generate` gives durations and `eval_count`. NVIDIA notebooks can use `nvidia-ml-py`.
- Grafana Cloud Free: 10k active series, 50 GB logs and 50 GB traces a month, 14-day retention, 3 users, no card,
  hard-limited. OTLP gateway with access-policy tokens scoped `metrics:write`, `logs:write`, `traces:write`.
  Alerting on the Free plan was UNVERIFIED at research time.
- Cloud Run request-based billing gives CPU only during requests (sidecars too), so background export between requests
  is throttled; SIGTERM gives about 10 s of CPU for a flush. Hence the Scheduler-driven flush (ADR 0027).
- Grafana reading Cloud Monitoring needs a service account key file (workload identity only on Grafana Cloud, for the
  signed-in user, with no alerting), which conflicts with "no stored keys". Platform metrics stay in the Google
  console (Q90).
- Decided: Q68 device, platform and per-job metrics; Q69 random install ID, friendly names only in Grafana value
  mappings, volunteers get per-job numbers only; Q84-Q87 unclaimable-jobs gauge, worker start line, cancel endpoint,
  install ID (ADR 0026); Q88-Q92 Grafana Cloud Free, a Scheduler flush each minute, platform metrics in the Google
  console, Alloy on the PC, one 90-day token per source (ADR 0027).

### Security review (step 7; Q93-Q95)
- Q93: a 4xx-flood alert goes to its own Pub/Sub topic and the kill switch's second, strict parser; drilled with a fake
  alert (about 90 s IAM propagation to the edge).
- Q94: the maintainer deactivates then deletes the static admin key; the bucket policy denies object actions on
  `inputs/*` and `results/*` to everyone but the broker role and the admin principal.
- Q95: the small-fixes list (security notes refresh, delete the unused sample service account, pin the service-level
  instance cap, `pip-audit` in CI, document dropped request logs and key-name exposure, lock Alloy's data directory,
  check a local database listening on all interfaces, `fleet-enqueue --data-class` required). See ADR 0028.
