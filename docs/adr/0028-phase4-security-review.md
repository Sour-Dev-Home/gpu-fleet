# 0028. Phase 4 security review: a flood trips the kill switch, a data-plane lock on the results bucket

**Status:** Accepted (maintainer, 2026-11-26, Q93-Q95)
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
2. **Data-plane lock (Q94).** The results bucket policy denies `s3:GetObject`, `PutObject` and `DeleteObject` (written since as the pattern `s3:*Object*`, which also covers the version, ACL
   and tagging variants; the broker's Allow is GetObject and PutObject, and `ListBucket` has no "Object" in it) on
   `inputs/*` and `results/*` to every principal except the broker role and the admin role
   (`var.admin_principal_arn`, sensitive, from the vault), matched with `ArnNotLike` on `aws:PrincipalArn` (an IAM
   user's user ARN, or a role's ARN, which is also what an assumed-role session reports; the maintainer signs in as
   an IAM user). Object actions only: Terraform's bucket management cannot be
   locked out. The TLS-only denies stay.
3. **Small fixes (Q95).** Service-level `scaling.max_instance_count` (the `maxScale` annotation) equals the revision cap
   for the broker and the kill switch; `pip-audit` over the three uv lockfiles in CI, with hash-pinned tooling;
   `setup-alloy.ps1` pins and locks Alloy's `--storage.path` under `%ProgramData%\fleet\alloy\data`;
   `fleet-enqueue --data-class` is required. Accepted trade-offs, written in docs/security.md: request logs below 500
   are excluded (privacy over forensics), and the broker's `ListBucket` exposes hashed key names (needed for a
   real 404 on HEAD).

4. **Kill switch hardening.** `/alert` accepts only Cloud Monitoring notification `version` "1.2"; the budget and
   alert shapes are exclusive (an `incident` makes a budget message invalid, budget fields make an alert invalid);
   ignore paths log `policy_matches` and `state` cut to 16 characters, never the attacker-chosen text. The flood
   condition counts 4xx except 429. The admin ARN may contain no `*`, and the bucket module's principal list has
   no default.
5. **Replay guard (K4).** An open incident whose `incident.started_at` (epoch seconds) is more than 1 hour old is
   logged and acknowledged (2xx), so a redelivered old message cannot undo a manual restore of public access. An
   undatable one (missing, not a number, NaN) is ignored the same way. This is deliberate: acting on a message
   we cannot date risks the same undo, while ignoring it loses little, because a real flood keeps the policy
   open and Monitoring re-notifies, and the budget path still backstops cost. The clock is injected for tests.
6. **Unknown paths return 404.** Known paths always answer 2xx on a bad message (redelivery can't fix it), but a
   path nothing handles means a misconfigured push endpoint; a 404 makes that loud (Pub/Sub retries, metrics show
   it) instead of silently acknowledging.

## Consequences
- A flood cuts public access in minutes; reset is `terraform apply` as for the budget (README, kill switch section).
  A false trip is possible if the threshold is hit by legitimate traffic: raise the threshold, not the switch's trust.
- The Monitoring service agent's email is UNVERIFIED until the first apply.
- The admin must run the probe script and any `aws s3` object command as the admin role; other principals get 403.
- Old Alloy storage under `%ProgramData%\GrafanaLabs` is abandoned (a few minutes of unsent metrics at most).

## Alternatives considered
- One topic for both payloads, told apart by shape: rejected, the two shapes should never share a parser.
- An allow-list-only bucket policy (`NotPrincipal`): rejected, it misbehaves with role sessions.
