# Phase 3 research (2026-11-25)

Gathered by the `researcher` agent. Items marked ✔ were re-checked by the coordinator. `[NEEDS VERIFICATION]`
means no fetched source supports it: check before relying on it.

| Topic | Finding | Source |
|---|---|---|
| S3 free tier amounts | [NEEDS VERIFICATION] New accounts get credits, not the old 5 GB / 20k GET / 2k PUT allowance. The pricing page mentions the first 100 GB/month of internet egress, not labelled as free tier. Check the Billing console's Free Tier page. | https://aws.amazon.com/s3/pricing/ |
| Free plan | New accounts are on a credits plan. The free plan closes after 6 months or when credits run out; S3 is "available on both plans". Credit lifetime conflicts: 6 months on /free/, 12 months on the S3 pricing page. | https://aws.amazon.com/free/ |
| Presigned URL expiry | IAM user with SigV4: up to 7 days. Role or STS credentials: ends when the session ends. | https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html |
| Presigned upload size limits | [NEEDS VERIFICATION] A PUT can pin an exact signed Content-Length; a POST policy supports `content-length-range`. SigV4 presigned PUT supports checksum headers (confirmed). | same page; S3 API reference |
| ✔ Terraform S3 native locking | `use_lockfile` is GA in Terraform 1.11.0 (2025-02-27). DynamoDB locking is deprecated. | terraform CHANGELOG v1.11.0 (re-checked) |
| State bucket IAM | List on the bucket path; Get/Put on the state object; Get/Put/Delete on the `.tflock` lock file. | https://developer.hashicorp.com/terraform/language/backend/s3 |
| Cloud Run → AWS | OIDC federation, no stored keys. Google is a built-in AWS identity provider (don't create one). `sts:AssumeRoleWithWebIdentity` with a Google-signed ID token. | https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html |
| Trust policy keys | `Federated: accounts.google.com`; conditions on `accounts.google.com:aud`, `:oaud`, `:sub`. Gotcha: when the token has `azp`, `:aud` holds `azp` and `:oaud` holds the token's `aud`. | IAM condition keys reference |
| Roles Anywhere | Not suited: needs X.509 certificates from your own CA. | Roles Anywhere user guide |
| ✔ AWS provider | 6.x; 6.74.0 published 2026-11-23. | registry.terraform.io (re-checked) |
| Minimum Terraform for provider 6.x | [NEEDS VERIFICATION] | — |

**The maintainer's account (2026-11-25, from the Billing console):** Free plan with credits; low free usage
shown; budget alerts already exist. **The plan closes around late January 2027.** Consequence: a Free-plan account closes 6 months after creation or when
credits run out, so nothing in AWS may be the only copy of anything (Terraform state included: the code
recreates it; results are content-addressed and recomputable).

Not checked: the Google side (minting a Cloud Run ID token with a chosen audience; the `sub` value), the current
latest Terraform release, current S3 free-tier numbers.
