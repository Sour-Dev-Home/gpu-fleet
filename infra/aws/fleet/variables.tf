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
