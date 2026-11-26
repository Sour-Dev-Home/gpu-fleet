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

variable "rotation_in_progress" {
  description = <<-EOT
    True only while rotating the broker's tokens: passes the two "-previous" secrets (which always exist, empty)
    to the broker as FLEET_*_TOKEN_PREVIOUS. Add a version to each first (README), or the revision can't start.
  EOT
  type        = bool
  default     = false
}
