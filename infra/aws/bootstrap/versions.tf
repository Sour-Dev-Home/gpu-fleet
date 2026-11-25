# Which Terraform and which providers this stack needs. `terraform init` downloads the providers and
# records their exact versions and checksums in .terraform.lock.hcl (committed, like uv.lock).

terraform {
  required_version = ">= 1.11" # 1.11: S3 native state locking (use_lockfile), used by the fleet stack

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0" # any 6.x; Dependabot proposes the next major
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # No backend block: this stack keeps its state in a local, git-ignored terraform.tfstate.
  # It creates the bucket that every other stack keeps its state in (ADR 0017).
}

provider "aws" {
  region = var.region

  # Refuse to run against any other account, whatever credentials happen to be loaded
  # (e.g. the [default] profile's keys from another project).
  allowed_account_ids = [var.account_id]

  # Tags on every resource this provider creates: you can find and cost everything by them.
  default_tags {
    tags = {
      project    = "gpu-fleet"
      managed-by = "terraform"
      stack      = "aws/bootstrap"
    }
  }
}
