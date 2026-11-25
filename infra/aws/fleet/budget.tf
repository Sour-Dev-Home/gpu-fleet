# The monthly cost budget created in the console, adopted with an `import` block (Q33; removed once
# applied). Written from the generated config, keeping only the arguments you're meant to set.

locals {
  # One list of thresholds instead of three copies of the same block.
  budget_alerts = [
    { type = "ACTUAL", percent = 85 },
    { type = "ACTUAL", percent = 100 },
    { type = "FORECASTED", percent = 100 },
  ]
}

resource "aws_budgets_budget" "monthly" {
  account_id   = var.account_id
  name         = "My Monthly Cost Budget"
  budget_type  = "COST"
  limit_amount = "30.0"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  metrics      = ["UnblendedCost"]

  # Kept as AWS set them, so the import shows no changes.
  billing_view_arn  = "arn:aws:billing::${var.account_id}:billingview/primary"
  time_period_start = "2026-11-01_00:00"
  time_period_end   = "2087-06-15_00:00"

  # Measure usage before credits and refunds pay for it: an alert fires on real spend even while
  # the account still has credits.
  filter_expression {
    not {
      dimensions {
        key    = "RECORD_TYPE"
        values = ["Credit", "Refund"]
      }
    }
  }

  dynamic "notification" {
    for_each = local.budget_alerts
    content {
      notification_type          = notification.value.type
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.percent
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
