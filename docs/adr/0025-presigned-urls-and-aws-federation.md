# 0025. Presigned URLs and AWS federation

**Status:** Accepted (maintainer, 2026-11-25, Q74-Q80)
**Date:** 2026-11-25
**Builds on:** [0002](0002-pull-based-broker.md), [0007](0007-http-contract-and-auth.md),
[0015](0015-data-class-clearance-capabilities.md), [0019](0019-public-broker-on-cloud-run.md) (Q40),
[0023](0023-broker-verifies-result-keys.md).

## Context
ADR 0002 promised that the broker hands out presigned S3 URLs so workers never hold cloud keys. Until now the
worker wrote results to a local directory and the broker took its word that they were stored. The broker runs
on Google Cloud (ADR 0019/0021) and S3 is on AWS, so it needs AWS credentials without a stored key (CLAUDE.md).

## Decision
- **One bucket, content-addressed prefixes (Q74).** `inputs/<input sha256>` and `results/<result key>` in the
  existing results bucket. A hash names its object, so there is no per-job path to guess or collide.
- **`/lease` carries download URLs (Q75).** In S3 mode the response adds `input_url` (and `context_url` when the job
  has a `context_sha256`): presigned GETs, 10 minutes. **`POST /upload-url`** (worker token plus the job's valid
  lease token; body `job_id, lease_token, result_key, sha256, size`) returns `{url, headers, expires_in}`: a
  presigned PUT of `results/<result key>`, 5 minutes. The broker recomputes the key (ADR 0023) and answers the
  same 422 `result_key_mismatch` to any other; it answers 413 `result_too_large` above `FLEET_MAX_RESULT_BYTES`
  (default 10 000 000, range-checked: a description is kilobytes, so more is a bug or an attack).
- **The upload is pinned (Q76).** The PUT signs `ContentLength` and `ChecksumSHA256` (base64 of the digest bytes)
  and `If-None-Match: *` (no overwrite); the returned `headers` are exactly the signed ones. **Whether S3 enforces
  each on a presigned PUT is unverified**, so `scripts/probe-presign.py` (run by the maintainer with their own
  login, against throwaway keys under `results/probe/`) reports it. The system does not depend on the answer:
  the declared `sha256` and `size` are stored on the job row at `/upload-url` (nullable columns, expand-only
  migration, ADR 0012), and `/complete` first HEADs `results/<key>` (ChecksumMode ENABLED) and answers
  **422 `result_missing`** (no such object) or **422 `result_mismatch`** (over the cap, a different length, a
  different or absent SHA-256, or nothing declared). Not 409: the lease is fine, and a retry sends the same
  thing. A refusal changes nothing; the worker leaves the lease to expire (the job is retried, then
  dead-lettered) and does not stop, unlike `result_key_mismatch`, which means its formula is wrong.
  A repeat `/complete` of an already done job skips the HEAD (it was verified the first time).
- **Every data class (Q77).** The clearance filter in the claim already decides who may hold a lease; whoever does
  gets URLs for that job's own keys. Never a caller-chosen key: the PUT URL is built from the broker's recomputed
  key, and the GET URLs from the job row.
- **No stored AWS keys (Q78).** The broker's Google service account asks the metadata server for an ID token for
  audience `FLEET_AWS_AUDIENCE`, then `sts:AssumeRoleWithWebIdentity` (1 hour, session `fleet-broker`) wrapped in
  botocore `RefreshableCredentials`, which renews before expiry. A small provider, not boto3's token-file mode
  (whether that re-reads a changed file is unverified). Presigning is local: no AWS call per lease.
  Terraform (`infra/aws/fleet/broker_role.tf`): role `fleet-broker`, trust principal `Federated:
  accounts.google.com` (built in to AWS: no OIDC provider resource) with three `StringEquals` conditions:
  `accounts.google.com:sub` is one of `var.broker_google_sa_unique_id` (a list, for phase 6's jobs account too;
  21 digits each, no default) AND `accounts.google.com:aud` (the token's `azp`, the same ID) is one of them,
  as defense in depth, AND `accounts.google.com:oaud` equals `var.federation_audience` (default
  `fleet-aws-federation`). The IDs are identifiers, not credentials, so the variable is not `sensitive` (that
  would hide the whole trust policy in every plan); they come from `TF_VAR_` only to stay out of git. Permissions: `s3:GetObject` on `inputs/*` and
  `results/*`, `s3:PutObject` on `results/*`, and `s3:ListBucket` on the bucket (condition `s3:prefix` StringLike
  `results/*`, `inputs/*`), nothing else. With ListBucket a missing key is a real 404: the broker reads **404 as "not
  stored"** and **403 as a fault** (a wrong role or bucket policy), answered like any storage trouble. **Unverified:**
  whether S3 evaluates the `s3:prefix` condition for HeadObject, which carries no prefix. If it does not, a HEAD of a
  missing key stays 403 and every first upload would answer 503 `storage_unavailable`: check with the role right
  after the apply (HEAD a key that does not exist) and drop the condition if so.
- **A result that is already stored wins.** A model's output is not deterministic, so a job rerun after a crash
  between PUT and `/complete` would otherwise meet its own earlier object (412, then `result_mismatch`, until
  dead-letter). `/upload-url` therefore HEADs the key first: if an object exists, is within the cap and has a
  checksum, the broker records THAT checksum and size as the job's declaration and answers
  `{"already_stored": true}` with no URL; the worker skips the PUT and completes, and `/complete` passes. A 412 on a
  PUT is handled the same way (ask again). The content-addressed key means any valid output for the same input,
  model and prompt is acceptable (ADR 0007's idempotency). An object that exists but has no checksum or is over
  the cap is not adopted; the job then fails at the PUT (412) and dead-letters, which needs a manual look.
- **Storage trouble does not cost attempts.** Credentials are resolved (`get_frozen_credentials`) before `/lease`
  claims, and at the start of `/upload-url`. An STS or metadata outage, a presign error, or a HEAD that cannot
  answer is **503 `storage_unavailable`** with `Retry-After`, the exception's type only in the log. Without this a
  claim would already have counted an attempt and a long outage would dead-letter the queue.
- **Local mode is unchanged (Q79).** Without `FLEET_RESULTS_BUCKET`, leases have exactly the old fields and
  `/complete` does not look at S3. `/upload-url` answers **404** with code `presigned_urls_disabled`: 409 already
  means "lease lost" to a worker (ADR 0007) and would make it skip the job, whereas 404 says "this broker has no
  such endpoint", which a worker reports as an error instead of mistaking it for contention. Setting some but not
  all of `FLEET_RESULTS_BUCKET`, `FLEET_AWS_ROLE_ARN`, `FLEET_AWS_AUDIENCE`, `AWS_REGION` is a startup error
  (`AWS_REGION` alone is not: machines set it).
- **The worker verifies before it runs (Q80).** When the lease carries URLs the worker uses a `UrlStore` bound to
  that lease: it streams the input (and context) from the URL, hashes it, and raises `InputMismatch` unless it
  equals `input_sha256` (`context_sha256`). Nothing is run, stored or completed; the lease expires. This runs for
  every runner, the echo runner included, inside the heartbeat so a large download cannot lose the lease. It
  uploads through `/upload-url` and a PUT with the returned headers (or skips it: see "already stored").
  `exists()` is always false (no presigned GET for results), so a retried job re-runs: the cost of keeping
  workers free of credentials. The downloads use a separate HTTP client without the broker's bearer token.
- **The worker accepts presigned URLs only for its own bucket host (SSRF).** Every URL (input, context, upload) must
  be `https`, on the default port, without userinfo, and its host must equal **`FLEET_RESULTS_HOST`**, exactly
  (case-insensitive). The host is worker configuration, not something the broker sends: a compromised broker could
  otherwise point workers at the cloud metadata server. The host is `<bucket>.s3.amazonaws.com` in us-east-1 (what
  boto3 signs, checked locally) and `<bucket>.s3.<region>.amazonaws.com` elsewhere. Unset means every URL is
  refused; `FLEET_ALLOW_HTTP` does not loosen it and loopback is no exception (a local S3 stand-in needs its own
  decision). Chosen over a host in the lease because a value from the party being defended against protects nothing.
- **Transient failures are retried while the lease is held.** Downloads (inside the heartbeat thread), the upload
  and `/complete` (503, 429, timeouts) retry up to 3 times after 1, 2 and 4 seconds, each up to 50% longer, and the
  Worker renews the lease between attempts. Only then does the loop's own backoff take over and the lease expire.
  `TransientError` messages carry the exception type, never the URL (it is a credential).

## Consequences
- The size cap is enforced at upload and again at completion (the config may change between).
- If S3 honours none of the signed constraints, a worker could still store a bad object; `/complete` refuses to
  record it and nothing points to it. Quarantine for volunteers (security.md, before phase 10) builds on this.
- Workers need `FLEET_RESULTS_HOST` set (infra/aws/README.md) before they can use a lease that carries URLs.
- Cloud Run needs `FLEET_RESULTS_BUCKET`, `FLEET_AWS_ROLE_ARN` (from `terraform output`), `FLEET_AWS_AUDIENCE` and
  `AWS_REGION`; that wiring is a follow-up PR (`infra/gcp` is not touched here).
- **Accepted logging gap:** CloudTrail event history keeps `AssumeRoleWithWebIdentity` for 90 days at no cost.
  There are no S3 data events and no alert on assume-role. A management trail to S3 is a later option.
- URL lifetimes (10 and 5 minutes) stay within botocore's 10-15 minute credential refresh margin, so a URL
  never outlives the credentials that signed it (tested).
- Cost: S3 requests at this scale are cents (a few thousand a month); STS is free.

## Alternatives considered
- **Workers with AWS keys, or a shared key in the image:** the thing ADR 0002 exists to avoid; revocation means
  rotating every worker.
- **The broker proxies every byte:** simple, but a Cloud Run request per transfer, billed and size-limited.
- **A stored AWS access key in Secret Manager for the broker:** works, but a long-lived secret; federation has none.
- **boto3's web identity token-file mode:** needs the ID token in a file kept fresh by something else.
- **A separate bucket or prefix per data class:** the data class is enforced at lease time; a second layer of
  storage policy can come with volunteers (phase 10) if quarantine needs it.
