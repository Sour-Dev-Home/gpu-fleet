# 0021. Cloud Run and Cloud SQL deployment: sidecar proxy, IAM login, public IP, no stored secrets

**Status:** Accepted (maintainer, 2026-11-25, Q56-Q65)
**Date:** 2026-11-25
**Builds on:** [0015](0015-data-class-clearance-capabilities.md), [0017](0017-terraform-aws-layout-and-state.md),
[0019](0019-public-broker-on-cloud-run.md), [0020](0020-postgres-job-store.md)

## Context
Phase 4 step 4 puts the broker on Cloud Run with its PostgreSQL store on Cloud SQL, as Terraform in
`infra/gcp/fleet/`. Cloud Run instances come and go, so the design avoids anything that must be stored, rotated
or kept warm: no database password, no keys, no always-on instance. Console prices are in
docs/research/phase-4.md.

## Decision
- **Sidecar Auth Proxy, IAM login (Q56).** A `cloud-sql-proxy` container (2.28.1, pinned by digest) shares the
  instance's network; the broker connects to `127.0.0.1:5432` with no password. `--auto-iam-authn` logs in as the
  service account `fleet-broker`; the database user is its email without `.gserviceaccount.com`. The account has
  only `cloudsql.client` and `cloudsql.instanceUser` (project level, narrowed by an IAM condition to this
  instance; the API accepting conditions on both roles is UNVERIFIED) and `secretmanager.secretAccessor` on the two secrets only.
- **Public IP, connectors required, TLS only (Q57).** `ipv4_enabled`, no authorized networks,
  `connector_enforcement = REQUIRED`, `ssl_mode = ENCRYPTED_ONLY`. The address is reachable but useless without
  the proxy and IAM. **Rejected for now:** private IP with Direct VPC egress (a /26 subnet, private services
  access, more to learn and to pay for, UNVERIFIED); revisit with GKE.
- **Bootstrap by runbook, not the postgresql provider (Q58).** One-time `GRANT` and `ALTER DATABASE ... OWNER`
  as `postgres` through a local proxy. No `root_password` in Terraform: the postgres password is set with gcloud
  and held in the vault, so it never enters state. The provider would need a network path from wherever Terraform
  runs and a password in state, for a step done once.
- **Secret containers in Terraform, values outside (Q59).** `fleet-worker-token` and `fleet-admin-token` exist
  with no versions in Terraform; the values are added with `gcloud secrets versions add` from the vault.
- **Local push, deploy by digest (Q60).** `docker build --platform linux/amd64`, push to Artifact Registry
  (keep 2 versions, drop untagged after 7 days, inside the 0.5 GB free tier), then `broker_image` set to
  `repo@sha256:...`. The variable refuses a tag. The service is created only when `broker_image` is set, so the
  first apply makes the registry the image goes into. CI deploys wait for the AWS federation work.
- **Cloud Run numbers (Q61).** Min 0, max 1 instance now and 3 once volunteers join (Q65; the compute and
  throughput ceiling, ADR 0019), 1 CPU and 512 MiB for the broker plus 1 CPU and 256 MiB for the proxy (Cloud Run
  accepts only whole CPUs above 1, so 1 + 0.25 was likely to be refused; 2 vCPU x about 43k active seconds is
  about 86k of the 180k free vCPU-s a month; 0.75 + 0.25 is untried),
  request-based billing, timeout 30 s, generation 1 (faster cold starts; nothing
  here needs generation 2). Concurrency 40: Little's law with 3 ms of SQL per request gives 40 x 0.003 = 0.12
  connections busy per instance, so the pool of 4 (ADR 0020) is ample even if every request hits the database;
  and at 3 instances 3 x 4 = 12 connections stay under the tier's limit (about 25, UNVERIFIED). Startup probes on `/healthz`
  and on the proxy's `/startup`; the broker starts after the proxy is ready. The cloud broker runs with
  `FLEET_WORKER_CLEARANCE=open` (ADR 0015): cloud workers are volunteers' stand-ins, and footage or derived
  text stay on the maintainer's own backends.
- **Maintenance (Q62):** Sunday 09:00 UTC (04:00 Eastern), stable track; backups 7 retained, daily 07:00 UTC,
  point-in-time recovery on (UNVERIFIED that `db-f1-micro` allows it; if apply refuses, backups only).
- **Request log excluded (Q64).** A project log exclusion drops Cloud Run's request log for this service. It
  records every caller's IP and user agent, and ADR 0018 promised that volunteers' and strangers' IPs are not
  stored. Only requests below status 500 are dropped: Cloud Run's own failures (no instance, timeouts) are
  kept, since the broker never sees them. A 4xx alert therefore has no request log behind it, by design: the
  broker's own logs say what happened. Database security logs (connections, disconnections, lock waits,
  checkpoints, temp files) are on: a few MB a month against 50 GiB free.
- **Denial of wallet (Q65).** The instance cap bounds compute and throughput, **not per-request fees**: an
  unauthenticated flood still costs per request. So: max instances 1 now (3 when volunteers join); a Cloud
  Monitoring alert emails when the 4xx rate stays above 5 requests/s for 5 minutes (the filter syntax is
  UNVERIFIED until seen in the console); a budget kill switch follows in a separate PR after the first deploy.
  Considered and deferred to phase 10: a private Cloud Run service with IAM invoker (Kaggle and Colab workers
  can't easily get Google ID tokens: unsolved), and a Cloudflare edge (airtight only with a load balancer or a
  private service behind it, since the run.app URL stays reachable otherwise).
- **Protection and the project ID (Q63).** Deletion protection on the instance (Terraform and API) and the
  service. The project ID is `TF_VAR_project_id` from the vault, never in a file. Provider labels use `gcp-fleet`
  (Google labels cannot contain `/`).

## Consequences
- Cost: Cloud SQL about $9/month (db-f1-micro $7.30 + 10 GB $1.70; console, 2026-11-25), Cloud Run inside the free
  tier at this traffic (about 0.43M requests/month when idle, against 2M free), Artifact Registry and Secret
  Manager free. The public IPv4 address charge is UNVERIFIED: check the bill. Cloud Monitoring alerting policies
  are reportedly no longer free (about $1.50/month per condition; one here): UNVERIFIED, check the first bill.
- The Auth Proxy is a second process in every instance: more memory (256 MiB) and one more image to bump by hand
  (Dependabot does not read `.tf`).
- The first deploy needs the manual steps in `infra/gcp/fleet/README.md`; the GRANT is UNTESTED until run.
- The public Cloud SQL IP is a standing finding for scanners (trivy GCP-0017): accepted and annotated.
- Teardown takes three flags off first (README step 10).

## Alternatives considered
- **Cloud SQL Python connector in the broker:** no sidecar, but a broker code change and a dependency; the proxy
  keeps the broker identical to the local one.
- **Password in Secret Manager, no IAM login:** one more secret to rotate and a password that could leak into logs.
- **Private IP and Direct VPC egress:** the stronger network posture; see Q57 above.
- **Terraform postgresql provider for grants:** see Q58 above.
