# Values go in terraform.tfvars (git-ignored), as for the bootstrap stack.

variable "account_id" {
  description = "The AWS account this stack may touch (12 digits). Kept out of git."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be the 12-digit AWS account ID."
  }
}

variable "alert_email" {
  description = "Where budget alerts go. Personal: kept in terraform.tfvars, hidden in plan output."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "broker_google_sa_unique_id" {
  description = "Unique IDs (numeric, not emails) of the Google service accounts that may assume the broker role: the broker's, later phase 6's jobs account. From TF_VAR_broker_google_sa_unique_id (a JSON list, from the vault), never a file: see README."
  type        = list(string)

  validation {
    condition     = length(var.broker_google_sa_unique_id) > 0 && alltrue([for id in var.broker_google_sa_unique_id : can(regex("^[0-9]{21}$", id))])
    error_message = "broker_google_sa_unique_id must be a non-empty list of 21-digit service account unique IDs (gcloud iam service-accounts describe <email> --format=\"value(uniqueId)\")."
  }
}

variable "admin_principal_arn" {
  description = "The maintainer's admin principal: with the broker's role, the only principals that may read, write or delete objects in the results bucket (Q94). An IAM user's aws:PrincipalArn is its user ARN (arn:aws:iam::<account>:user/<path and name>); a role session's is the role's ARN (an SSO role's includes its path). Never an assumed-role session ARN (arn:aws:sts::). Get it with `aws sts get-caller-identity --query Arn`. From TF_VAR_admin_principal_arn (vault), never committed; contains the account ID."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:(user|role)/[A-Za-z0-9+=,.@_/*-]+$", var.admin_principal_arn))
    error_message = "admin_principal_arn must be an IAM user or role ARN: arn:aws:iam::<12 digits>:user/<path and name> or :role/<path and name> (not an assumed-role session ARN)."
  }
}

variable "federation_audience" {
  description = "The audience the broker requests in its Google ID token; the role's trust policy requires it (accounts.google.com:oaud). Not a secret: a label both sides agree on."
  type        = string
  default     = "fleet-aws-federation"

  validation {
    condition     = can(regex("^[A-Za-z0-9._:/-]{3,100}$", var.federation_audience))
    error_message = "federation_audience must be 3-100 characters from letters, digits and . _ : / -"
  }
}
