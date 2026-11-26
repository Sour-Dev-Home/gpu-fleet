# 0024. Budget kill switch

**Status:** Accepted (maintainer, 2026-11-25, Q70-Q73)
**Date:** 2026-11-25
**Builds on:** [0019](0019-public-broker-on-cloud-run.md), [0021](0021-cloud-run-and-cloud-sql-deployment.md)

## Context
ADR 0021 (Q65) capped instances but not per-request fees, and a 4xx alert only tells us afterwards. A flood of
unauthenticated requests costs money until someone notices. A budget alert only emails; nothing stops the spend.

## Decision
- **What it does (Q70).** When actual spend reaches 100% of a $20 monthly budget, `allUsers` is removed from
  `roles/run.invoker` on the broker. Further requests get Google's 403 before reaching the container. Remove public
  access rather than delete the service or the database: it is one reversible IAM change, it keeps the data, and it
  stops request fees at the front door. Cloud SQL (about $9 a month) keeps running: this caps variable cost, it is
  not a full stop.
- **$20 (Q71).** Normal running is about $10-11 (Cloud SQL alone about $9), so $20 leaves room for a busy month
  and fires on a real anomaly. The budget is on this project only, gross of credits (`EXCLUDE_ALL_CREDITS`):
  with about $296 of credits a net figure would stay near $0 and never fire. Rules at 50% and 90% email the billing
  admins; 100% is the trigger. The variable `budget_usd` changes it.
- **Design (Q72): push to a private Cloud Run service.** Budget -> Pub/Sub topic `fleet-budget` -> push subscription
  with an OIDC token -> `fleet-killswitch`, a small Python service in our own image (`killswitch/`) that edits the
  broker's IAM policy (read, remove `allUsers`, write with the etag; one retry on a conflict). It is private (only the
  push account `fleet-budget-pusher` is an invoker), max 1 instance, scale to zero. It decides from `costAmount >=
  budgetAmount`, not from `alertThresholdExceeded`, which is unreliable; notifications arrive several times a day
  whether or not a threshold was crossed.
- **Least privilege.** Its account `fleet-killswitch` holds a custom role `fleetKillSwitch` with exactly
  `run.services.getIamPolicy` and `run.services.setIamPolicy`, bound on the broker service only. The only predefined
  role with `setIamPolicy` is `roles/run.admin`, which can also deploy and delete. A custom role in a per-service
  binding is UNVERIFIED until the first apply.
- **Authentication is Cloud Run IAM, not the app (accepted; security review Low 5).** The service does no JWT check
  of its own: only principals with `roles/run.invoker` on it (the push account) get past Google's front end. A
  forged message from anyone holding publisher on the topic or a project-level invoker role can only make it
  remove public access, which is the fail-safe direction (it cannot grant anything, and `terraform apply`
  restores the binding). Accepted, so no in-app token verification.
- **Status codes.** 2xx acknowledges; anything else makes Pub/Sub redeliver with backoff. A malformed message or one
  below the threshold gets 200: the same bytes can never become valid, and the next notification carries fresh
  numbers. An IAM failure (including a second etag conflict) gets 500: the switch has not fired, and redelivery is
  the retry. No dead-letter topic: nothing it could hold is worth keeping.
- **Why not Cloud Functions + Cloud Build + Eventarc (Q72).** A function deploy pulls in Cloud Build, Artifact
  Registry builds and an Eventarc trigger: three more APIs and service agents to learn and secure, for 100 lines of
  code. Our own container reuses the broker's pipeline (pinned base, uv, non-root, digest deploy) and Pub/Sub push
  with OIDC is the same mechanism Eventarc uses underneath.
- **Reset (Q73).** The broker's `allUsers` binding is a Terraform resource, so `terraform apply` restores it. A plan
  showing that binding "to add" means the switch fired. Fix the cause, raise `budget_usd` if the spend was legitimate,
  apply. If spend is still at the budget the next notification trips it again.

- **Gross spend (Q81, maintainer, 2026-11-25).** `EXCLUDE_ALL_CREDITS`: net of the ~$296 credits the figure stays
  about $0 and would never fire, and the credits are what pay for phases 6-7. It also counts free-tier usage as
  spend. Runbook: confirm in the billing report, with credits excluded, that normal running is about $10-11 gross
  before trusting $20. `currency_code = "USD"` must match the billing account's currency. Design limit: the switch
  does not stop Cloud SQL's always-on cost.
- **Accidental reset.** Any `terraform apply` re-adds the broker's `allUsers` binding. A plan showing
  `google_cloud_run_v2_service_iam_member.public` "to add", when you did not intend a reset, means the switch fired:
  stop and investigate first. A Terraform `check "broker_is_public"` reads the broker's real IAM policy and warns
  at plan time when `allUsers` is absent (a warning, never an error; quiet before the broker exists).
- **Accepted residual risk, with detection.** A per-service `setIamPolicy` can grant any role on the broker, so a
  compromised switch could grant itself or others access. A log-match alert emails on every Admin Activity
  `SetIamPolicy` on the broker by the switch's account. A filter cannot tell an add from a remove, and each one
  should be rare and expected, so all are reported (filter syntax confirmed by the drill).

## Drill (2026-11-26, after the first apply of 14 resources)
A fake notification (`costAmount` 21, `budgetAmount` 20) published to the topic. Within about 2 minutes the switch
logged "budget reached: public access removed", `allUsers` was gone from the broker's invoker role, `GET /nope`
returned Google's 403 instead of the broker's JSON 404, and the IAM-change alert email arrived. The restore plan
showed exactly the `public` binding to add, with the `broker_is_public` warning; the apply restored it and `/nope`
returned the broker's 404 again. Confirmed by it: the custom role works on a per-service binding, the OIDC push and
the Pub/Sub token-creator grant, and the log-match filter. Still unproven: that Cloud Billing itself can publish
(only a real notification shows it). Lesson: probe with a GET; a bodyless POST gets Google's 411 before any IAM
check, which hid the 403 at first.

## Consequences
- **Delay.** Budget data lags by hours, and the first notification can take several. The switch caps damage; it
  does not stop the first dollar. The instance cap and rate limit still bound the burn rate meanwhile.
- Cost: ~$0 (UNVERIFIED): budgets are believed free; Pub/Sub and Cloud Run stay inside their free tiers at a few
  messages a day. Check the first bill.
- Two more APIs (`billingbudgets`, `pubsub`), the provider's `billing_project` / `user_project_override`, and a
  `TF_VAR_billing_account` in the vault (sensitive, never committed).
- UNVERIFIED until a real notification arrives: that Cloud Billing's publisher is
  `billing-budget-alert@system.gserviceaccount.com`. The drill proved the push, token and IAM edit.
- Drilling makes the broker unreachable for about a minute.

## Alternatives considered
- A Cloud Function triggered by the topic: see above.
- Disabling billing on the project: stops everything and is hard to reverse; rejected.
- Deleting or scaling the broker to zero: scale-to-zero costs nothing but still bills requests that reach it;
  removing public access stops them earlier.
