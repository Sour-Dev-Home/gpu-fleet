output "results_bucket" {
  description = "Where workers upload results through presigned URLs (phase 4)."
  value       = module.results.name
}

output "broker_role_arn" {
  description = "FLEET_AWS_ROLE_ARN for the broker (Cloud Run wiring is a follow-up). Contains the account ID: do not commit it."
  value       = aws_iam_role.broker.arn
}
