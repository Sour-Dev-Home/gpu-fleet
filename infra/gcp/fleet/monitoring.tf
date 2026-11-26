# Q65: a flood of unauthenticated requests costs per request even though the instance cap bounds compute. A
# burst of 4xx (wrong or missing tokens, scans, rate-limit 429s) is the sign, so it sends an email. The budget
# kill switch that would act on it is a separate PR after the first deploy.
# Cost UNVERIFIED: alerting policies are reportedly no longer free (about $1.50/month per condition). Check the
# first bill; one condition here.
resource "google_monitoring_notification_channel" "email" {
  display_name = "fleet alerts (email)"
  type         = "email"

  labels = {
    email_address = var.alert_email # sensitive: from TF_VAR_alert_email, never committed
  }

  depends_on = [google_project_service.api]
}

resource "google_monitoring_alert_policy" "broker_4xx" {
  display_name = "fleet-broker: 4xx request rate"
  combiner     = "OR"

  conditions {
    display_name = "4xx responses above 5/s for 5 minutes"

    condition_threshold {
      # UNVERIFIED filter syntax: written from the metric list (run.googleapis.com/request_count, label
      # response_code_class); validate can't check it, so look at the policy in the console after apply.
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"fleet-broker\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"4xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM" # all instances and revisions together
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "More than 5 client-error responses per second for 5 minutes on fleet-broker: token guessing, a scan, or a broken worker. See docs/security.md."
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.api]
}

# Residual risk of the kill switch (ADR 0024): its custom role can set the broker's IAM policy, so a compromised
# switch could grant any role on the broker. Each SetIamPolicy by its account should be rare and expected (only
# when the budget trips), so any one is emailed. A log filter cannot tell an add from a remove, so it alerts on
# every one. Admin Activity audit logs are always on. Filter syntax UNVERIFIED until the drill fires it.
resource "google_monitoring_alert_policy" "killswitch_iam_change" {
  display_name = "fleet kill switch changed the broker's IAM policy"
  combiner     = "OR"

  conditions {
    display_name = "SetIamPolicy on the broker by the kill switch account"

    condition_matched_log {
      filter = <<-EOT
        logName:"cloudaudit.googleapis.com%2Factivity"
        protoPayload.methodName:"SetIamPolicy"
        protoPayload.resourceName:"services/fleet-broker"
        protoPayload.authenticationInfo.principalEmail="${google_service_account.killswitch.email}"
      EOT
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "300s" # required for log-match alerts
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  depends_on = [google_project_service.api]
}
