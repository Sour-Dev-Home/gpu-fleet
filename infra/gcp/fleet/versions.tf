terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.12"
    }
  }

  # State lives in the AWS state bucket (ADR 0019, Q41), so running this stack needs the AWS login as well
  # as Google credentials. A backend block can't use variables, so the values are written out; none is secret.
  backend "s3" {
    bucket       = "fleet-tfstate-190d94"
    key          = "gcp/fleet/terraform.tfstate" # one key per stack: they never share a state file
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # writes terraform.tfstate.tflock beside the state while a plan or apply runs
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # The budget API is called with the maintainer's own credentials (no keys), which need a project to bill the
  # call to: without these two, google_billing_budget fails with user credentials (provider docs). The user needs
  # serviceusage.services.use on the project (an Owner has it).
  user_project_override = true
  billing_project       = var.project_id

  # Google label values allow only lowercase letters, digits, "_" and "-", so the AWS stack's
  # "aws/fleet" becomes "gcp-fleet" here.
  default_labels = {
    project    = "gpu-fleet"
    managed-by = "terraform"
    stack      = "gcp-fleet"
  }
}
