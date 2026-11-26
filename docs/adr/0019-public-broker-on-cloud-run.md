# 0019. The broker goes public on Cloud Run: app tokens, keyless AWS access, phase 4 order

**Status:** Accepted (maintainer, 2026-11-25, Q39-Q43)
**Date:** 2026-11-25
**Builds on:** [0002](0002-pull-based-broker.md), [0007](0007-http-contract-and-auth.md),
[0011](0011-broker-auth-hardening.md), [0017](0017-terraform-aws-layout-and-state.md)

## Context
Phase 4 moves the broker from localhost to Google Cloud Run, where anyone on the internet can reach it
(docs/security.md, threat model 4-9). Workers are deliberately varied: the local RX 9070, Kaggle and Colab
notebooks (phase 5), and later volunteers (phase 10). Presigned S3 URLs need AWS credentials on the broker,
and the project rule is no stored cloud keys. The Google Cloud billing account is upgraded (paid), holds
about $296 of credits, and has a $20 budget alert made in the console. A budget alert notifies; it doesn't
stop spending.

## Decision
- **Public URL, the app's own tokens (Q39).** Cloud Run allows unauthenticated invocation; the broker's
  worker and admin bearer tokens stay the gate. Google IAM auth on the URL would need a Google ID token on
  every request, which notebooks and volunteers can't easily get.
- **Keyless AWS access now, not in phase 6 (Q40).** The broker's service account presents a Google-signed
  ID token to AWS STS (`AssumeRoleWithWebIdentity`) for a role that only it can assume, allowed only to read
  and write objects in the results bucket. Phase 6 reuses the trust for Cloud Run Jobs.
- **Terraform state for the Google Cloud stack (Q41)** lives in the existing S3 state bucket under
  `gcp/fleet/terraform.tfstate`: one versioned, locked, TLS-only store. Running that stack needs the
  `aws login` session as well as Google credentials.
- **Order (Q42), one PR each:** (1) security pre-work locally: hashed lease tokens, two-token rotation,
  per-token rate limits; (2) a networked job store (its own ADR, after docs/research/phase-4.md);
  (3) console walkthrough: a hello-world Cloud Run service and the budget; (4) Terraform for Google Cloud:
  APIs, Artifact Registry, Secret Manager, the Cloud Run broker, the budget; (5) AWS federation and presigned
  URLs; (6) telemetry on Cloud Run; (7) security review, then a local worker leasing from the cloud broker.

### Step 1 as built (2026-11-25)
- **Lease tokens** are stored as `sha256(token)`; only the claim's response carries the token itself.
- **Rotation:** `FLEET_WORKER_TOKEN_PREVIOUS` and `FLEET_ADMIN_TOKEN_PREVIOUS` are also accepted, with the same
  length and ASCII checks, and no token may serve both roles. Every configured token is compared, in constant
  time, with no early exit. The trace propagator accepts both worker tokens.
- **Rate limit:** in-memory token buckets on the worker endpoints, 20/s with bursts of 100
  (`FLEET_WORKER_RATE`, `FLEET_WORKER_BURST`, range-checked so `nan` or `inf` can't switch it off),
  checked after authentication. Heartbeats have their own bucket, so a `/lease` hot loop can't cost
  running jobs their leases. Per instance, so the max-instances cap (step 4) sets the fleet-wide
  ceiling. Workers treat 429 like 503: back off and retry.
- **Body cap (Q46):** chunked bodies are counted as they arrive and refused past 16 KiB, in the app,
  instead of Cloud Armor and its load balancer.
- **Job store (Q44-Q45, decided):** Cloud SQL for PostgreSQL, used everywhere (Compose and tests too);
  details in step 2's own ADR.

## Consequences
- The Cloud Run max-instances cap and the rate limits bound throughput and compute, not the whole bill:
  unauthenticated requests are still billed per request (docs/security.md, open item). Budgets only alert. The
  existing console budget is adopted into Terraform by import, as with AWS (ADR 0017).
  *Amended 2026-11-25: billing wording corrected (see docs/security.md).*
- Every request from the internet reaches the app, so the token check must stay cheap and constant-time,
  and rate limits apply after authentication.
- ADR 0017's "Google OIDC trust in phase 6" moves to phase 4.

## Alternatives considered
- **Cloud Run IAM auth:** stops strangers before the app runs, but every worker would need Google
  credentials. That blocks notebooks and volunteers.
- **AWS access keys in Secret Manager:** simple, but long-lived keys are what the project rules exclude.
- **A GCS state bucket with its own bootstrap stack:** more to learn, but a second state store to secure
  for no gain at this size.
