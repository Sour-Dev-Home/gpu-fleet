# The fleet's AWS resources (ADR 0017). Phase 3: the results bucket. Phase 4: the broker's role (broker_role.tf).

resource "random_id" "suffix" {
  byte_length = 3
}

# Results are content-addressed: a key's bytes never change, so old versions would add nothing.
module "results" {
  source     = "../modules/private-bucket"
  name       = "fleet-results-${random_id.suffix.hex}"
  versioning = false
}
