# 0027. Telemetry destination: Grafana Cloud Free over OTLP, write-only tokens, a scheduled flush

**Status:** Accepted (maintainer, 2026-11-26, Q88-Q92)
**Date:** 2026-11-26
**Builds on:** [0018](0018-observability-opentelemetry.md), [0021](0021-cloud-run-and-cloud-sql-deployment.md),
[0024](0024-budget-kill-switch.md), [0026](0026-telemetry-signals.md).

## Context
ADR 0018 made every component speak OTLP and kept a local Grafana LGTM stack for development; ADR 0026 added the
signals worth watching. Nothing yet reads them from the cloud broker, and the maintainer's PC and workers need a
place to send to as well. Cloud Run is billed per request (CPU only while a request runs, ADR 0021), which starves
the SDK's background exporter between requests.

## Decision
- **Destination (Q88).** Grafana Cloud Free, OTLP/HTTP, basic auth: user = the stack's OTLP instance ID, password =
  a token. The Python exporter reads the header from `OTEL_EXPORTER_OTLP_HEADERS` and wants it as
  `Authorization=Basic%20<base64>` (a literal space splits the header). The local LGTM stack stays for development.
- **Write-only tokens, one per source** (broker, PC Alloy, PC worker), scopes `metrics:write`, `logs:write`,
  `traces:write`, 90-day expiry. A leaked one can add noise to our free quota and nothing else.
- **The broker (Q89).** Terraform makes the Secret Manager container `fleet-grafana-token-broker` (the maintainer adds
  the version from the vault, so no secret enters state), readable by the broker's service account on that secret
  only. The OTLP endpoint and instance ID are sensitive variables fed by `TF_VAR_` from the vault, passed to the
  broker only when `grafana_export` is true (default false, so the stack plans without them). The broker builds the
  header at startup and never logs it.
- **Flush instead of an always-on CPU.** `POST /telemetry/flush` collects the gauges once and force-flushes metrics,
  traces and logs (10 s cap in total). Cloud Scheduler calls it every minute: 43,200 requests a month against the
  2M free tier, and Scheduler's 3 free jobs. Request-based billing stays; `cpu_idle = false` (always-on CPU) would
  cost far more than the free tier absorbs. A minute is also the metrics' resolution.
- **Flush auth.** EITHER the admin bearer token OR a Google-signed ID token whose audience is the fixed string
  `fleet-telemetry-flush`, whose issuer is Google, and whose email is the scheduler's service account with
  `email_verified` true (google-auth, certificates cached an hour). No admin token is stored in Scheduler. The
  string is a Cloud Run custom audience on the broker service and is the Scheduler's token audience and the
  broker's `FLEET_FLUSH_OIDC_AUDIENCE`, so nothing depends on a predicted `run.app` URL. Why a custom audience:
  while `allUsers` is bound, Cloud Run does not check the token at all, so a wrong audience would stay hidden and
  surface only once the kill switch removed `allUsers` and flushes started failing, during the incident.
- **Kill switch.** The scheduler account keeps `roles/run.invoker` on the broker as its own additive member, so
  flushes work while the switch has removed `allUsers`: telemetry matters most during an incident, and the call is
  authenticated twice (Google IAM, then the broker). The switch removes only `allUsers`, and the stack has no
  authoritative IAM resource on the service, so neither undoes the other.
- **Platform metrics (Q90)** (Cloud Run, Cloud SQL) stay in the Google Cloud console: exporting them needs a
  collector or a scraper that would cost more than the console is worth at this scale.
- **PC (Q91) and rotation (Q92).** Grafana Alloy on the PC scrapes the Windows exporter (cpu, memory, logical_disk,
  net, gpu) and forwards over OTLP, the token read from an ACL-protected file; the worker gets the endpoint and header
  for one process from the vault. One token per source, 90-day expiry, rotation documented in the stack's README.

## Consequences
- The broker wakes once a minute, around the clock, so it never scales to zero for long: about 43,200 short requests
  a month, well inside the free tier but no longer idle-free. Cloud SQL is touched by each collection (3 queries).
- Cloud Scheduler is a new API and a new service account. A flush failing is silent except in the logs and in gaps
  on the dashboard; the existing alerts do not cover it.
- Token expiry is a calendar chore: an expired token looks like "no data".
- UNVERIFIED until the first apply: Scheduler's OIDC to Cloud Run with the dedicated account, the custom audience
  being accepted by the broker's own verification, and Grafana's free-tier limits under this load.

## Alternatives considered
- **Keys with broad scopes:** one token for everything. Rejected: a leak from the PC would expose the cloud's too.
- **`cpu_idle = false` (always-on CPU):** exports on its own timer. Rejected: two containers at 1 vCPU for a month
  is about 5M vCPU-seconds against 180k free, for a once-a-minute need. The flushes cost about 0.3 s x 2 vCPU x
  43,200 = 26k vCPU-seconds (napkin, unmeasured).
- **The admin token in Scheduler:** simpler, but puts a high-value secret in one more place. Rejected for OIDC.
- **Platform metrics through a collector:** rejected (see Q90 above).
- **Scheduler without invoker while public access is off:** flushes would 403 exactly during an incident. Rejected.
