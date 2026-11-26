# The broker's AWS identity (ADR 0025). No access keys exist: the broker (a Google Cloud service
# account on Cloud Run) asks Google for an ID token and swaps it at STS for an hour of credentials
# (AssumeRoleWithWebIdentity). accounts.google.com is a built-in identity provider in AWS, so there is
# no aws_iam_openid_connect_provider to create.

data "aws_iam_policy_document" "broker_trust" {
  statement {
    sid     = "GoogleServiceAccountsOfTheFleet"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = ["accounts.google.com"]
    }

    # :sub is the service account's unique ID. Only these identities, not "any Google account".
    condition {
      test     = "StringEquals"
      variable = "accounts.google.com:sub"
      values   = var.broker_google_sa_unique_id
    }

    # :aud is the token's authorized party (azp), the same service account ID: defense in depth, so a
    # token must name the right account in both claims.
    condition {
      test     = "StringEquals"
      variable = "accounts.google.com:aud"
      values   = var.broker_google_sa_unique_id
    }

    # :oaud is the audience the token was requested for. A token minted for another service, even
    # by the same service account, is refused.
    condition {
      test     = "StringEquals"
      variable = "accounts.google.com:oaud"
      values   = [var.federation_audience]
    }
  }
}

resource "aws_iam_role" "broker" {
  name                 = "fleet-broker"
  description          = "The fleet broker on Cloud Run: signs presigned URLs for the results bucket."
  assume_role_policy   = data.aws_iam_policy_document.broker_trust.json
  max_session_duration = 3600
}

# Presigning is local, but a URL only works if its signer may do the operation. Nothing else: no
# delete, no ACLs. Inputs are read-only for the broker; results are the only place it may write
# through a URL.
data "aws_iam_policy_document" "broker_permissions" {
  statement {
    sid       = "ReadInputsAndResults"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.results.arn}/inputs/*", "${module.results.arn}/results/*"]
  }

  # With ListBucket, S3 answers 404 for a key that does not exist; without it, 403. The broker treats
  # 404 as "not stored" and 403 as a fault (storage_unavailable), so a wrong role or bucket policy is loud.
  # Limited to the two prefixes the fleet uses.
  statement {
    sid       = "ListOurPrefixes"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.results.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["results/*", "inputs/*"]
    }
  }

  statement {
    sid       = "WriteResults"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${module.results.arn}/results/*"]
  }
}

resource "aws_iam_role_policy" "broker" {
  name   = "results-bucket"
  role   = aws_iam_role.broker.id
  policy = data.aws_iam_policy_document.broker_permissions.json
}
