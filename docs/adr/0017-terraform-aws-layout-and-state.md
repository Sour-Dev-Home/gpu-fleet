# 0017. Terraform for AWS: two stacks, S3 state with native locking, keyless sign-in

**Status:** Accepted (maintainer, 2026-11-25, Q20-Q26)
**Date:** 2026-11-25
**Builds on:** [0002](0002-pull-based-broker.md) (AWS is coordination only), [0003](0003-content-addressed-results.md)

## Context
Phase 3 puts the first real resources in AWS: a bucket for Terraform state and one for job results. Terraform
needs somewhere to keep its state, and that place can't be created by the stack that stores its state in it.
The maintainer's machine already has AWS profiles for another project, including a `[default]` profile with
long-lived access keys. The AWS account is on the Free plan, which closes around late January 2027 unless
upgraded (decided: upgrade in early January; docs/research/phase-3.md).

## Decision
- **Sign-in:** `aws login --profile fleet` (temporary console-session credentials, refreshed by the CLI); no
  access keys for this project. Terraform runs with `AWS_PROFILE=fleet`. Every AWS provider pins
  `allowed_account_ids`, so credentials for another account are refused.
- **Two stacks:** `infra/aws/bootstrap/` (local, git-ignored state) creates the state bucket;
  `infra/aws/fleet/` keeps its state there with `use_lockfile = true` (S3 native locking, Terraform >= 1.11;
  no DynamoDB table).
- **Buckets differ by data:** the state bucket has versioning with old versions expiring after 90 days and
  `prevent_destroy`; the results bucket has no versioning, because content-addressed keys never change. Both:
  SSE-S3, all public access blocked, ACLs off, incomplete uploads aborted after 7 days, HTTPS (TLS 1.2+) only.
- **Names:** `fleet-tfstate-<random>` and `fleet-results-<random>`, committed. The account ID stays in
  git-ignored `terraform.tfvars`.
- **IAM arrives with its first use:** phase 3 creates buckets and a zero-spend budget; the broker role comes in
  phase 4, the Google OIDC trust in phase 6.
- **Checks:** CI runs `terraform fmt -check`, `terraform validate` (no backend, no credentials), tflint and
  `trivy config`; Dependabot has a `terraform` entry. Every `apply` follows a `plan` reviewed by the
  infra-reviewer agent and the maintainer.

## Consequences
- No AWS secret exists on disk for this project; a stolen laptop yields at most a session that expires.
- If bootstrap's local state is lost, `apply` would create a second bucket: rebuild it with `terraform import`
  (infra/aws/README.md) instead.
- Accepted scanner findings: no customer-managed KMS key (KMS bills per request) and no access logging (a
  second bucket for one user). Revisit if anyone else gets access.
- A Free-plan closure deletes everything after 90 days. Nothing in AWS is the only copy: the code recreates
  the buckets, and results can be recomputed.

## Alternatives considered
- **Access keys for an IAM user:** the simplest, and the leak the exam warns about. Rejected.
- **IAM Identity Center (`aws sso login`):** the team-grade choice; more setup than one person needs while
  `aws login` gives the same short-lived credentials.
- **One stack with local state:** loses state with the laptop and has no locking.
- **DynamoDB locking:** deprecated since Terraform 1.11.
- **Versioning on results:** costs storage for versions that can never differ.
