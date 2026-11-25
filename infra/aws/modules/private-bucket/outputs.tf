output "name" {
  value = aws_s3_bucket.this.bucket
}

output "arn" {
  description = "For IAM policies (phase 4: the broker's presign role)."
  value       = aws_s3_bucket.this.arn
}
