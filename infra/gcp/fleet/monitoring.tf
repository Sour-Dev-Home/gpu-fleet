# Q65: a flood of unauthenticated requests costs per request even though the instance cap bounds compute. A
# burst of 4xx (wrong or missing tokens, scans, rate-limit 429s) is the sign, so it sends an email. The budget
# kill switch that would act on it is a separate PR after the first deploy.
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
