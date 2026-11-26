# Q64: Cloud Run's request log records every caller's IP address and user agent. This service is public and will
# serve volunteers and strangers, so those are not stored (ADR 0018's promise about IPs). The broker's own
# logs, and the errors Cloud Run reports, are untouched; only the request log is dropped.
# Cost side effect: 50 GiB of logging is free and nothing here is near it, so this is privacy, not savings.
resource "google_logging_project_exclusion" "request_log" {
  name        = "fleet-broker-request-log"
  description = "Drop Cloud Run request logs for fleet-broker: they hold client IPs and user agents."
  filter      = "resource.type=\"cloud_run_revision\" AND log_name=\"projects/${var.project_id}/logs/run.googleapis.com%2Frequests\" AND resource.labels.service_name=\"fleet-broker\""

  depends_on = [google_project_service.api]
}
