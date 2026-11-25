# Inputs. Values go in terraform.tfvars (git-ignored); infra/aws/README.md shows the shape.

variable "account_id" {
  description = "The AWS account this stack may touch (12 digits). Kept out of git."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be the 12-digit AWS account ID."
  }
}

variable "region" {
  description = "Region for the state bucket."
  type        = string
  default     = "us-east-1"
}
