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
