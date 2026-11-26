# The fleet's AWS resources (ADR 0017). Phase 3: the results bucket. Phase 4: the broker's role (broker_role.tf).

resource "random_id" "suffix" {
  byte_length = 3
}

# Results are content-addressed: a key's bytes never change, so old versions would add nothing.
module "results" {
  source     = "../modules/private-bucket"
  name       = "fleet-results-${random_id.suffix.hex}"
  versioning = false

  # Q94: only the broker's role and the maintainer's admin role touch objects (see the module). The probe script
  # and `aws s3` on this bucket must run as the admin role, or they get 403.
  data_allowed_principal_arns = [aws_iam_role.broker.arn, var.admin_role_arn]
}
