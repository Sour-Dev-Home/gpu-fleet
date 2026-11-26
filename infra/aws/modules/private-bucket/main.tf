# A private, encrypted, HTTPS-only S3 bucket: the settings every fleet bucket shares (ADR 0017).
# A module is a folder of .tf files called with inputs, like a function. The caller decides the name
# and whether to keep versions; the security settings aren't optional.

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# Accepted (ADR 0017): no access logging; a log bucket for one user isn't worth it at this scale.
#trivy:ignore:AVD-AWS-0089
resource "aws_s3_bucket" "this" {
  bucket = var.name
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Accepted (ADR 0017): off for the results bucket; content-addressed keys never change, so versions add nothing.
#trivy:ignore:AVD-AWS-0090
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    # "Disabled" is only valid for a bucket that never had versioning; "Suspended" is how you turn it off.
    status = var.versioning ? "Enabled" : "Disabled"
  }
}

# Accepted (ADR 0017): SSE-S3 rather than a customer-managed KMS key.
#trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket     = aws_s3_bucket.this.id
  depends_on = [aws_s3_bucket_versioning.this]

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Only when versioning is on: a `dynamic` block repeats its content once per item in for_each,
  # so an empty list means the rule isn't written at all.
  dynamic "rule" {
    for_each = var.versioning ? [1] : []
    content {
      id     = "expire-old-versions"
      status = "Enabled"
      filter {}
      noncurrent_version_expiration {
        noncurrent_days = var.noncurrent_days
      }
      expiration {
        expired_object_delete_marker = true
      }
    }
  }
}

data "aws_iam_policy_document" "tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
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

  statement {
    sid       = "DenyOldTls"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
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

  # Q94: a data-plane lock. Nobody but the listed roles may read, write or delete objects under inputs/ and
  # results/, whatever their IAM policies say (an explicit Deny beats any Allow, so a leaked or over-broad
  # identity in this account still cannot reach the data).
  # - aws:PrincipalArn of an assumed-role session (SSO, `aws login`, the broker's web-identity session) is the
  #   ROLE's ARN, not the session ARN (arn:aws:sts::...:assumed-role/<role>/<session>), so list role ARNs. An
  #   SSO role's ARN includes its path (role/aws-reserved/sso.amazonaws.com/<region>/AWSReservedSSO_...);
  #   ArnNotLike accepts * wildcards for the part that varies.
  # - A presigned URL is evaluated as its SIGNER (the broker role), so workers' uploads are unaffected.
  # - Object actions only: bucket-level calls (policy, versioning, ListBucket, the Terraform plan/apply) are not
  #   denied, so Terraform cannot lock itself out and an admin can always fix a wrong list by editing the policy.
  dynamic "statement" {
    for_each = length(var.data_allowed_principal_arns) > 0 ? [1] : []
    content {
      sid     = "DenyDataAccessExceptAllowedRoles"
      effect  = "Deny"
      actions = ["s3:*Object*"]
      resources = [
        "${aws_s3_bucket.this.arn}/inputs/*",
        "${aws_s3_bucket.this.arn}/results/*",
      ]
      principals {
        type        = "*"
        identifiers = ["*"]
      }
      condition {
        test     = "ArnNotLike"
        variable = "aws:PrincipalArn"
        values   = var.data_allowed_principal_arns
      }
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket     = aws_s3_bucket.this.id
  policy     = data.aws_iam_policy_document.tls_only.json
  depends_on = [aws_s3_bucket_public_access_block.this]
}
