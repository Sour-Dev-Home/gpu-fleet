# infra/aws

AWS is coordination only: S3 for Terraform state and job results (ADR 0002, ADR 0017). Two stacks:

| Stack | State | Creates |
|---|---|---|
| `bootstrap/` | local `terraform.tfstate` (git-ignored) | the state bucket |
| `fleet/` | in the state bucket, locked with `use_lockfile` | results bucket, budget (phase 3); broker role (phase 4) |

## Sign in (no access keys)

```powershell
aws configure set region us-east-1 --profile fleet   # once: the profile's default region (not a secret)
aws login --profile fleet                        # browser sign-in; short-lived, auto-refreshed credentials
aws sts get-caller-identity --profile fleet      # check: your IAM user's ARN (don't paste it anywhere public)
$env:AWS_PROFILE = "fleet"                       # for this shell only; Terraform reads it
```

Each stack needs a `terraform.tfvars` (git-ignored) with your account ID. The provider refuses any other
account, so stray credentials (another project's `[default]` profile) can't be used by mistake. From
`infra/aws/fleet`, this writes both without copying the ID by hand:

```powershell
$id = aws sts get-caller-identity --profile fleet --query Account --output text
foreach ($d in '../bootstrap', '.') { Set-Content "$d/terraform.tfvars" "account_id = `"$id`"" }
```

## Every change: plan, review, apply

```powershell
terraform init       # once per stack, and after provider changes
terraform plan "-out=tfplan"     # quoted: PowerShell splits -flag=name.ext at the dot
# review: the infra-reviewer agent, then you (CLAUDE.md)
terraform apply tfplan
```

## The broker's role (`fleet/broker_role.tf`, ADR 0025)

The broker runs on Google Cloud and signs S3 URLs, so it needs AWS credentials without a stored key. The role
`fleet-broker` trusts `accounts.google.com` (built in to AWS: no provider resource) for tokens whose subject is
one of your service accounts' numeric IDs AND whose audience is `federation_audience`. It may read `inputs/open/*` and
`results/open/*` and write `results/open/*` of the results bucket (the data class is in the key, Q82), nothing else.

Apply steps (from `infra/aws/fleet`, signed in as above). The unique ID is not the email; it is not secret in
the way a key is, but it stays out of git:

```powershell
$sa = gcloud iam service-accounts describe <broker service account email> --format="value(uniqueId)"
# keep it in the local vault (docs/secrets.md), then load it for this shell, as a JSON list:
$env:TF_VAR_broker_google_sa_unique_id = "[`"$sa`"]"   # phase 6 adds the jobs account's ID to this list
terraform init
terraform plan "-out=tfplan"     # review with the infra-reviewer agent, then apply as above
terraform apply tfplan
terraform output broker_role_arn   # FLEET_AWS_ROLE_ARN: contains the account ID, so never commit it
```

**Object lock (Q94).** The results bucket's policy denies `s3:GetObject`, `PutObject` and `DeleteObject` on `inputs/*`
and `results/*` to every principal except the broker role and your admin principal, so a leaked or over-broad identity in
the account cannot reach the data. Set the admin principal first (in the vault, never committed). Get the value
you sign in as, store it (prompted, never on the command line), and pass it through `scripts/with-secrets.ps1`
alongside `TF_VAR_broker_google_sa_unique_id`:

```powershell
aws sts get-caller-identity --profile fleet --query Arn --output text   # arn:aws:iam::<account>:user/<name>
Set-Secret -Vault fleet -Name TF_VAR_admin_principal_arn                # paste the ARN at the prompt
```

An IAM user's `aws:PrincipalArn` is its user ARN (`arn:aws:iam::<account>:user/<path>/<name>`); a role session's is
the role's ARN, never the session ARN (`arn:aws:sts::...:assumed-role/<role>/<session>`, which the variable rejects).
An SSO role's ARN includes its path (`role/aws-reserved/sso.amazonaws.com/<region>/AWSReservedSSO_...`).
Only object actions are denied, so Terraform (which manages the bucket and never reads objects) cannot lock itself
out, and a wrong value is fixed by re-applying as admin. The probe script below must run as that admin principal (a
session of any other principal gets 403 on the control upload). Presigned uploads are evaluated as the broker
role that signed them, so workers are unaffected.

Workers in S3 mode need `FLEET_RESULTS_HOST` = `<bucket>.s3.amazonaws.com` (us-east-1; `<bucket>.s3.<region>.amazonaws.com`
elsewhere): they refuse presigned URLs for any other host. After the apply, check once with the role (not your
login) that a HEAD of a key that does not exist answers 404, not 403 (ADR 0025: ListBucket on the bucket has no
condition, so S3 says 404 for a missing key; a 403 means the role or bucket policy is wrong).

The Cloud Run wiring (`FLEET_RESULTS_BUCKET`, `FLEET_AWS_ROLE_ARN`, `FLEET_AWS_AUDIENCE`, `AWS_REGION`) is a
follow-up change in `infra/gcp`; until then the broker runs in local mode.

Before relying on the signed constraints, measure what S3 enforces on a presigned PUT. It writes only throwaway
keys under `results/probe/` and deletes them, using your own login (from the repository root):

```powershell
$env:AWS_REGION = "us-east-1"
# `aws login` sessions need botocore[crt], which the broker doesn't ship: hand the session over as env vars instead
aws configure export-credentials --profile fleet --format powershell | Invoke-Expression
uv run --directory broker python ../scripts/probe-presign.py --bucket (terraform -chdir=infra/aws/fleet output -raw results_bucket)
Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN, Env:AWS_CREDENTIAL_EXPIRATION -ErrorAction SilentlyContinue
```

Last run (2026-11-26): ENFORCED on all three (403 longer body, 400 other SHA-256, 412 overwrite).

Three lines say ENFORCED or NOT ENFORCED for: a longer body than signed, a body with another SHA-256, and a
second PUT to the same key. Not enforced is safe (`/complete` HEADs the object), but tell the maintainer.

## If bootstrap's local state is lost

Running `apply` again would create a *second* bucket with a new random suffix. Instead, rebuild the state
from the real bucket (its name is in the `backend "s3"` block of `fleet/versions.tf`; the suffix is its last 6 characters):

```powershell
terraform import random_id.suffix <suffix-as-base64url>   # e.g. "a1b2c3" hex -> "obLD"
terraform import aws_s3_bucket.state fleet-tfstate-<suffix>
terraform plan   # imports the rest's differences as changes: review before applying
```
