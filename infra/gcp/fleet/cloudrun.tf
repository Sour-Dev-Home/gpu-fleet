# The broker on Cloud Run (ADR 0021). Created only once an image exists: the first apply leaves broker_image
# null, so the registry exists to push to before the service is asked to pull from it.
resource "google_cloud_run_v2_service" "broker" {
  count = var.broker_image == null ? 0 : 1

  name     = "fleet-broker"
  location = var.region

  # Public: anyone can reach the URL, and the broker's own bearer tokens are the gate (ADR 0019, Q39).
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account = google_service_account.broker.email

    # Gen1: faster cold starts, and this broker needs nothing gen2 adds (full Linux, network file systems).
    # It scales to zero, so a cold start is paid often.
    execution_environment = "EXECUTION_ENVIRONMENT_GEN1"

    timeout                          = "30s" # the broker's longest call is a few ms of SQL
    max_instance_request_concurrency = 40    # the rate limit allows 20/s per instance; see ADR 0021 on the pool

    scaling {
      min_instance_count = 0 # $0 when idle, at the price of a cold start
      max_instance_count = var.max_instances
    }

    containers {
      name  = "broker"
      image = var.broker_image

      # Start only after the proxy's startup probe passes, so the first request finds the database door open.
      depends_on = ["cloud-sql-proxy"]

      ports {
        container_port = 8000 # the Dockerfile's uvicorn port; no code change needed for Cloud Run
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true # request-based billing: CPU only while a request is handled
      }

      env {
        name = "FLEET_DATABASE_URL"
        # No password: the proxy logs in with the service account's token. "@" in the user name is "%40".
        value = "postgresql://${urlencode(local.db_user)}@127.0.0.1:5432/fleet"
      }
      env {
        # Cloud workers are volunteers' stand-ins: open data only (ADR 0015, ADR 0019). Footage and derived
        # text stay on the maintainer's own backends, which use a different token and broker.
        name  = "FLEET_WORKER_CLEARANCE"
        value = "open"
      }
      env {
        name = "FLEET_WORKER_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.token["fleet-worker-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "FLEET_ADMIN_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.token["fleet-admin-token"].secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8000
        }
        period_seconds    = 2
        timeout_seconds   = 2
        failure_threshold = 15
      }
    }

    containers {
      name = "cloud-sql-proxy"
      # Cloud SQL Auth Proxy 2.28.1 (looked up 2026-11-25 with `docker buildx imagetools inspect`; this is the
      # multi-platform index digest). Dependabot's docker ecosystem does not read .tf files: bump by hand.
      image = "gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.28.1@sha256:c01857183fd88d7f4366500f8f89e79206d5dba596d1b4eada8974082c162000"

      args = [
        "--auto-iam-authn", # log in with the service account's token: no database password
        "--lazy-refresh",   # refresh certificates on demand: CPU is throttled between requests
        "--health-check",
        "--http-address=0.0.0.0", # so Cloud Run's probe can reach it
        "--http-port=9090",
        "--port=5432",
        google_sql_database_instance.fleet.connection_name,
      ]

      resources {
        limits = {
          cpu    = "1" # under 1 CPU Cloud Run forces concurrency 1 (UNVERIFIED for a sidecar): keep it whole
          memory = "256Mi"
        }
        cpu_idle = true
      }

      startup_probe {
        http_get {
          path = "/startup"
          port = 9090
        }
        period_seconds    = 2
        timeout_seconds   = 2
        failure_threshold = 15
      }
    }
  }

  depends_on = [
    google_project_service.api,
    google_project_iam_member.sql_client,
    google_project_iam_member.sql_instance_user,
    google_secret_manager_secret_iam_member.broker_reads,
    google_sql_user.broker,
  ]
}

# allUsers may call the URL; the application checks the bearer token (Q39). An organization policy that
# forbids allUsers would fail this apply: say so rather than weaken it.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.broker_image == null ? 0 : 1

  name     = google_cloud_run_v2_service.broker[0].name
  location = google_cloud_run_v2_service.broker[0].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
