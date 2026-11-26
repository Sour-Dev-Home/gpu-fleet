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
one of your service accounts' numeric IDs AND whose audience is `federation_audience`. It may read `inputs/*` and
`results/*` and write `results/*` of the results bucket, nothing else.

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

The Cloud Run wiring (`FLEET_RESULTS_BUCKET`, `FLEET_AWS_ROLE_ARN`, `FLEET_AWS_AUDIENCE`, `AWS_REGION`) is a
follow-up change in `infra/gcp`; until then the broker runs in local mode.

Before relying on the signed constraints, measure what S3 enforces on a presigned PUT. It writes only throwaway
keys under `results/probe/` and deletes them, using your own login (from the repository root):

```powershell
$env:AWS_REGION = "us-east-1"
uv run --directory broker python ../scripts/probe-presign.py --bucket (terraform -chdir=infra/aws/fleet output -raw results_bucket)
```

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
