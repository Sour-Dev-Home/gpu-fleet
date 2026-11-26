# Telemetry to Grafana Cloud Free over OTLP/HTTP (ADR 0027, Q88-Q92), and the once-a-minute flush that makes it
# work on Cloud Run with request-based billing.
#
# - The broker's write-only Grafana token lives in Secret Manager as a CONTAINER here; the maintainer adds the
#   version from the vault (README), so no secret enters Terraform state.
# - The OTLP endpoint and the stack's instance ID come from TF_VAR_ variables (the vault), like project_id, and
#   are only passed to the broker when var.grafana_export is true: the stack plans and applies without them.
# - Cloud Run gives CPU only while a request runs, so the SDK's background exporter starves between requests.
#   Cloud Scheduler calls POST /telemetry/flush every minute with an OIDC token of its own service account (no
#   admin token anywhere in Scheduler); the broker verifies it (oidc.py) and force-flushes.
#
# KILL SWITCH (killswitch.tf, ADR 0024): the switch removes only `allUsers` from the broker's invoker role. The
# scheduler account keeps its OWN run.invoker member (scheduler_invokes below), so flushes, and with them the
# telemetry that explains an incident, keep working while the public is locked out. Both are additive member
# resources: no authoritative binding or policy exists in this stack, so neither apply nor the switch removes
# the other's member. The broker still checks the ID token itself (audience, email, email_verified).

locals {
  # The flush token's audience. A custom audience on the broker service (cloudrun.tf, custom_audiences) lets
  # Cloud Run accept ID tokens minted for this string, so nothing depends on a predicted run.app URL. One value,
  # used three times: the service's custom_audiences, the Scheduler's oidc_token audience and the broker's
  # FLEET_FLUSH_OIDC_AUDIENCE (a test pins that they are the same). It is a name, not a secret.
  flush_audience = "fleet-telemetry-flush"

  # The job needs a broker to call and the export switched on.
  flush_enabled = var.grafana_export && var.broker_image != null

  # Names are listed apart from the values: the values can be sensitive, and for_each can't iterate a
  # sensitive collection. Empty when the export is off, so the broker's env is exactly as before.
  # Only NON-sensitive values live here. One sensitive value inside the service's env set would make every env
  # entry "(sensitive value)" in plans and the broker's env could not be reviewed; the endpoint and the instance
  # ID therefore reach the broker as secret references (below), a test pins that no env value uses a sensitive
  # variable.
  grafana_env_names = var.grafana_export ? toset([
    "FLEET_FLUSH_OIDC_AUDIENCE",
    "FLEET_FLUSH_OIDC_EMAIL",
  ]) : toset([])

  grafana_env = {
    FLEET_FLUSH_OIDC_AUDIENCE = local.flush_audience
    FLEET_FLUSH_OIDC_EMAIL    = google_service_account.scheduler.email
  }
}

# --- the stack's account identifiers, as secret versions ---------------------------------------------------------

# The OTLP endpoint and the instance ID are identifiers, not secrets (the token is the secret). They are stored
# here WITH their versions, so the values sit in Terraform state: fine, the state is private (ADR 0027). Going
# through Secret Manager keeps them out of plan text (sensitive variables) without making the broker's whole env
# sensitive. The broker reads them pinned to these versions: the ordering is guaranteed and a change rolls a
# new revision.
resource "google_secret_manager_secret" "grafana_otlp_endpoint" {
  count = var.grafana_export ? 1 : 0

  secret_id = "fleet-grafana-otlp-endpoint"

  replication {
    auto {}
  }

  depends_on = [google_project_service.api]
}

resource "google_secret_manager_secret_version" "grafana_otlp_endpoint" {
  count = var.grafana_export ? 1 : 0

  secret      = google_secret_manager_secret.grafana_otlp_endpoint[0].id
  secret_data = var.grafana_otlp_endpoint

  # A changed value makes a new version: create it before destroying the old one, which the running revision
  # still pins (min instances 0, so a cold start in between would fail).
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "broker_reads_grafana_otlp_endpoint" {
  count = var.grafana_export ? 1 : 0

  secret_id = google_secret_manager_secret.grafana_otlp_endpoint[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.broker.email}"
}

resource "google_secret_manager_secret" "grafana_instance_id" {
  count = var.grafana_export ? 1 : 0

  secret_id = "fleet-grafana-instance-id"

  replication {
    auto {}
  }

  depends_on = [google_project_service.api]
}

resource "google_secret_manager_secret_version" "grafana_instance_id" {
  count = var.grafana_export ? 1 : 0

  secret      = google_secret_manager_secret.grafana_instance_id[0].id
  secret_data = var.grafana_instance_id

  lifecycle {
    create_before_destroy = true # see grafana_otlp_endpoint
  }
}

resource "google_secret_manager_secret_iam_member" "broker_reads_grafana_instance_id" {
  count = var.grafana_export ? 1 : 0

  secret_id = google_secret_manager_secret.grafana_instance_id[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.broker.email}"
}

# --- the broker's Grafana token ------------------------------------------------------------------------------

# Container only. Add the version from the vault (README, "Grafana Cloud export").
resource "google_secret_manager_secret" "grafana_token_broker" {
  secret_id = "fleet-grafana-token-broker"

  replication {
    auto {}
  }

  depends_on = [google_project_service.api]
}

# On this secret only: the broker's other secrets keep their own bindings (main.tf).
resource "google_secret_manager_secret_iam_member" "broker_reads_grafana" {
  secret_id = google_secret_manager_secret.grafana_token_broker.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.broker.email}"
}

# --- the flush schedule --------------------------------------------------------------------------------------

resource "google_service_account" "scheduler" {
  account_id   = "fleet-scheduler"
  display_name = "Fleet Cloud Scheduler (flushes the broker's telemetry)"

  depends_on = [google_project_service.api]
}

# Per service, not per project, and kept while allUsers is removed (see the KILL SWITCH note on top).
resource "google_cloud_run_v2_service_iam_member" "scheduler_invokes" {
  count = local.flush_enabled ? 1 : 0

  name     = google_cloud_run_v2_service.broker[0].name
  location = google_cloud_run_v2_service.broker[0].location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# 43,200 calls a month (one a minute) against Cloud Run's 2M free requests; Scheduler's first 3 jobs are free.
resource "google_cloud_scheduler_job" "telemetry_flush" {
  count = local.flush_enabled ? 1 : 0

  name             = "fleet-telemetry-flush"
  description      = "Force the broker to export its telemetry (ADR 0027)"
  region           = var.region
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "30s"

  # No retries, on purpose: a missed minute is covered by the next one, and a retry would stack a second flush
  # on the first. Every field is stated at the value the API stores (its defaults, read back from the live job
  # on 2026-11-26): with retry_count alone, or with no block, the plan flip-flops between + and - retry_config.
  retry_config {
    retry_count          = 0
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
    max_retry_duration   = "0s"
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.broker[0].uri}/telemetry/flush"

    # No body and no Authorization header of our own: Scheduler adds the Google-signed ID token.
    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.flush_audience
    }
  }

  depends_on = [
    google_project_service.api,
    google_cloud_run_v2_service_iam_member.scheduler_invokes,
  ]
}
