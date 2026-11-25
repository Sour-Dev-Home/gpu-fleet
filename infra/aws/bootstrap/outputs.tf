# Printed after apply; `terraform output` shows them again. The fleet stack's backend needs these.

output "state_bucket" {
  description = "Name of the Terraform state bucket."
  value       = aws_s3_bucket.state.bucket
}

output "region" {
  description = "Region of the state bucket."
  value       = var.region
}
