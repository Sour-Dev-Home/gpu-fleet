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

## If bootstrap's local state is lost

Running `apply` again would create a *second* bucket with a new random suffix. Instead, rebuild the state
from the real bucket (its name is in the `backend "s3"` block of `fleet/versions.tf`; the suffix is its last 6 characters):

```powershell
terraform import random_id.suffix <suffix-as-base64url>   # e.g. "a1b2c3" hex -> "obLD"
terraform import aws_s3_bucket.state fleet-tfstate-<suffix>
terraform plan   # imports the rest's differences as changes: review before applying
```
