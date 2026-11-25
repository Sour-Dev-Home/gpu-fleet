# The Terraform state bucket (ADR 0017). Every setting you clicked in the console walkthrough is
# written out here, so the bucket doesn't depend on whatever AWS's defaults are in a given year.

# A random suffix: bucket names are global across all AWS accounts, and this avoids putting the
# account ID in a name that appears in URLs. Generated once, then kept in state.
resource "random_id" "suffix" {
  byte_length = 3 # 6 hex characters
}

# Accepted findings (ADR 0017): SSE-S3 rather than a customer-managed KMS key (KMS bills per request and
# adds nothing at this scale), and no access logging (a second bucket plus log storage, for one user).
#trivy:ignore:AVD-AWS-0089
resource "aws_s3_bucket" "state" {
  bucket = "fleet-tfstate-${random_id.suffix.hex}"

  # Terraform refuses to plan a destroy of this bucket. Losing it loses every stack's state.
  lifecycle {
    prevent_destroy = true
  }
}

# ACLs off: only IAM and the bucket policy decide access (console: "ACLs disabled").
resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Console: "Block all public access", all four on.
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Every state write keeps the previous version, so a bad apply can be rolled back.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3: encrypted at rest, free. (SSE-KMS adds key control and an audit trail, at a cost per request.)
#trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  # Versioning must be on before old versions can expire.
  depends_on = [aws_s3_bucket_versioning.state]

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {} # every object in the bucket

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    # Every apply writes and deletes a .tflock file; with versioning, each delete leaves a "delete
    # marker". Once its old versions expire, the marker is clutter: remove it.
    expiration {
      expired_object_delete_marker = true
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Refuse any request that isn't over HTTPS. State can hold sensitive values.
data "aws_iam_policy_document" "state_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # HTTPS, but not with the old TLS 1.0/1.1 protocols.
  statement {
    sid     = "DenyOldTls"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_tls_only.json

  # A bucket policy is refused while public-access settings are still being applied.
  depends_on = [aws_s3_bucket_public_access_block.state]
}
