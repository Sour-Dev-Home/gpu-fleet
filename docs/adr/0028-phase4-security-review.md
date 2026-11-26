# 0028. Phase 4 security review: a flood trips the kill switch, a data-plane lock on the results bucket

**Status:** Proposed (maintainer's decisions Q93-Q95, 2026-11-26; awaiting approval of the ADR)
**Date:** 2026-11-26
**Builds on:** [0024](0024-budget-kill-switch.md), [0025](0025-presigned-urls-and-aws-federation.md),
[0027](0027-telemetry-destination.md).

## Context
A security review of the deployed phase 4 stack found three gaps: the budget kill switch reacts to spend that lags
by hours, so a flood of 4xx requests (billed per request) can run for hours first; the results bucket is protected
by IAM allows only, so an over-broad identity in the account could read or delete objects; and several smaller
items (stale docs, a service-level instance cap not stated, no dependency audit in CI, an unlocked Alloy storage
directory, a default data class on `fleet-enqueue`).

## Decision
1. **Flood alert (Q93).** The existing 4xx alert (more than 5 client errors/s, summed, for 5 minutes) also notifies
   a Pub/Sub channel publishing to a new topic `fleet-flood`. Its own push subscription calls the kill switch's
   `/alert` path with the same pusher account and OIDC. The switch has a second, strict parser: it acts only on
   `incident.state == "open"` with `incident.policy_name` equal to `FLEET_FLOOD_POLICY` (the policy's display name,
   from Terraform); anything else is logged and acknowledged, never raised. The budget path `/` and the alert path
   accept only their own payload shape. Only the Cloud Monitoring notification service agent may publish, on that
   topic only. A worker with a wrong token polls about once a second, well under the threshold.
2. **Data-plane lock (Q94).** The results bucket policy denies `s3:GetObject`, `PutObject` and `DeleteObject` on
   `inputs/*` and `results/*` to every principal except the broker role and the admin role
   (`var.admin_role_arn`, sensitive, from the vault), matched with `ArnNotLike` on `aws:PrincipalArn` (a role's ARN,
   which is also what an assumed-role session reports). Object actions only: Terraform's bucket management cannot be
   locked out. The TLS-only denies stay.
3. **Small fixes (Q95).** Service-level `scaling.max_instance_count` (the `maxScale` annotation) equals the revision cap
   for the broker and the kill switch; `pip-audit` over the three uv lockfiles in CI, with hash-pinned tooling;
   `setup-alloy.ps1` pins and locks Alloy's `--storage.path` under `%ProgramData%\fleet\alloy\data`;
   `fleet-enqueue --data-class` is required. Accepted trade-offs, written in docs/security.md: request logs below 500
   are excluded (privacy over forensics), and the broker's `ListBucket` exposes hashed key names (needed for a
   real 404 on HEAD).

## Consequences
- A flood cuts public access in minutes; reset is `terraform apply` as for the budget (README, kill switch section).
  A false trip is possible if the threshold is hit by legitimate traffic: raise the threshold, not the switch's trust.
- The Monitoring service agent's email is UNVERIFIED until the first apply.
- The admin must run the probe script and any `aws s3` object command as the admin role; other principals get 403.
- Old Alloy storage under `%ProgramData%\GrafanaLabs` is abandoned (a few minutes of unsent metrics at most).

## Alternatives considered
- One topic for both payloads, told apart by shape: rejected, the two shapes should never share a parser.
- An allow-list-only bucket policy (`NotPrincipal`): rejected, it misbehaves with role sessions.
