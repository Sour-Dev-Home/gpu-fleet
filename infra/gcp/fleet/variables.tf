# No terraform.tfvars here: the project ID stays out of git and files. It comes from the environment:
#   ./scripts/with-secrets.ps1 -Names TF_VAR_project_id,TF_VAR_alert_email -- terraform plan
# (TF_VAR_project_id lives in the local vault; see infra/gcp/fleet/README.md).

variable "project_id" {
  description = "The Google Cloud project this stack builds in. From TF_VAR_project_id; never committed."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a Google Cloud project ID (6-30 lowercase letters, digits, hyphens)."
  }
}

variable "region" {
  description = "One region for everything. us-central1 is a Tier 1 region and has the Cloud Run free tier."
  type        = string
  default     = "us-central1"
}

variable "broker_image" {
  description = <<-EOT
    The broker image, pinned by digest (<registry path>@sha256:...). Null on the first apply: that makes the
    registry, the database and the secrets, and no Cloud Run service, so the image can be pushed before the
    second apply. A tag can be moved after review, a digest cannot.
  EOT
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.broker_image == null ? true : can(regex("@sha256:[0-9a-f]{64}$", var.broker_image))
    error_message = "broker_image must end in @sha256: followed by 64 hex digits (a digest, not a tag)."
  }
}

variable "killswitch_image" {
  description = <<-EOT
    The kill switch image, pinned by digest (<registry path>/killswitch@sha256:...). Null until it is pushed: the
    topic, the budget and the accounts exist without it, and the service and its subscription wait (like broker_image).
  EOT
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.killswitch_image == null ? true : can(regex("@sha256:[0-9a-f]{64}$", var.killswitch_image))
    error_message = "killswitch_image must end in @sha256: followed by 64 hex digits (a digest, not a tag)."
  }
}

variable "billing_account" {
  description = "The billing account ID (XXXXXX-XXXXXX-XXXXXX) the budget is created on. From TF_VAR_billing_account in the vault; never committed, hidden in plan output."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account))
    error_message = "billing_account must look like 012345-6789AB-CDEF01."
  }
}

variable "budget_usd" {
  description = "Monthly budget in whole US dollars. At 100% of it the kill switch removes public access to the broker (ADR 0024). Normal running is about $10-11."
  type        = number
  default     = 20

  validation {
    condition     = var.budget_usd >= 1 && floor(var.budget_usd) == var.budget_usd
    error_message = "budget_usd must be a whole number of dollars, at least 1."
  }
}

variable "maintenance_utc_hour" {
  description = "Hour (UTC) of the Sunday Cloud SQL maintenance window. 9 = 04:00 US Eastern in winter, 05:00 in summer."
  type        = number
  default     = 9

  validation {
    condition     = var.maintenance_utc_hour >= 0 && var.maintenance_utc_hour <= 23 && floor(var.maintenance_utc_hour) == var.maintenance_utc_hour
    error_message = "maintenance_utc_hour must be a whole hour from 0 to 23."
  }
}

variable "max_instances" {
  description = <<-EOT
    Cloud Run's cap on instances: it bounds compute, the fleet-wide rate limit (the token buckets are per
    instance) and the database connections (instances x pool of 4 must stay under about 25; ADR 0020). It
    does not bound per-request fees. 1 for now (Q65); it rises to 3 when volunteers join (ADR 0021).
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.max_instances >= 1 && var.max_instances <= 6
    error_message = "max_instances must be 1-6: past 6 the 4-connection pools can exceed the database's limit (ADR 0020)."
  }
}

variable "alert_email" {
  description = "Where the 4xx-rate alert goes. Personal: from TF_VAR_alert_email in the vault, never committed, hidden in plan output."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "s3_mode" {
  description = "Presigned S3 URLs and AWS federation for the broker (ADR 0025). False removes all four S3 settings together: a rollback without a code change."
  type        = bool
  default     = true
}

variable "aws_federation_audience" {
  description = "The audience of the broker's Google ID token for AWS. Must equal federation_audience in infra/aws/fleet (a test compares the defaults). Not a secret."
  type        = string
  default     = "fleet-aws-federation"
}

variable "rotation_in_progress" {
  description = <<-EOT
    True only while rotating the broker's tokens: passes the two "-previous" secrets (which always exist, empty)
    to the broker as FLEET_*_TOKEN_PREVIOUS. Add a version to each first (README), or the revision can't start.
  EOT
  type        = bool
  default     = false
}
