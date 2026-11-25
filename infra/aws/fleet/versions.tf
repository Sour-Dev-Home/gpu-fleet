terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # State lives in the bucket the bootstrap stack created. A backend block can't use variables (it's read
  # before anything else), so the values are written out. None of them is secret.
  backend "s3" {
    bucket       = "fleet-tfstate-190d94"
    key          = "aws/fleet/terraform.tfstate" # one key per stack: they never share a state file
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # writes terraform.tfstate.tflock beside the state while a plan or apply runs
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      project    = "gpu-fleet"
      managed-by = "terraform"
      stack      = "aws/fleet"
    }
  }
}
