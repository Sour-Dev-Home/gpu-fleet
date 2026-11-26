# Q64: Cloud Run's request log records every caller's IP address and user agent. This service is public and will
# serve volunteers and strangers, so those are not stored (ADR 0018's promise about IPs). The broker's own
# logs are untouched. Requests with status 500 and above are kept: they include Cloud Run's own failures (no
# instance available, timeouts), which the broker never sees. By design a 4xx alert therefore has no request
# log behind it: it says something is wrong, and the broker's own logs say what.
# Cost side effect: 50 GiB of logging is free and nothing here is near it, so this is privacy, not savings.
resource "google_logging_project_exclusion" "request_log" {
  name        = "fleet-broker-request-log"
  description = "Drop Cloud Run request logs below 500 for fleet-broker: they hold client IPs and user agents."
  filter      = "resource.type=\"cloud_run_revision\" AND log_id(\"run.googleapis.com/requests\") AND resource.labels.service_name=\"fleet-broker\" AND httpRequest.status < 500"

  depends_on = [google_project_service.api]
}
