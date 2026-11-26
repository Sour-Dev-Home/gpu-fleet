# The broker on Cloud Run (ADR 0021). Created only once an image exists: the first apply leaves broker_image
# null, so the registry exists to push to before the service is asked to pull from it.
# The AWS stack's outputs (infra/aws/fleet), read from the same state bucket as this stack's backend
# (versions.tf). Skipped when s3_mode is false, so a rollback does not need the AWS state.
data "terraform_remote_state" "aws" {
  count   = var.s3_mode ? 1 : 0
  backend = "s3"

  config = {
    bucket = "fleet-tfstate-190d94"
    key    = "aws/fleet/terraform.tfstate"
    region = "us-east-1"
  }
}

locals {
  # Set together or not at all: the broker treats a partial set as a startup error.
  s3_env = var.s3_mode ? {
    FLEET_RESULTS_BUCKET = data.terraform_remote_state.aws[0].outputs.results_bucket
    FLEET_AWS_ROLE_ARN   = data.terraform_remote_state.aws[0].outputs.broker_role_arn
    FLEET_AWS_AUDIENCE   = var.aws_federation_audience
    AWS_REGION           = "us-east-1"
  } : {}
}

resource "google_cloud_run_v2_service" "broker" {
  count = var.broker_image == null ? 0 : 1

  name     = "fleet-broker"
  location = var.region

  # Public: anyone can reach the URL, and the broker's own bearer tokens are the gate (ADR 0019, Q39).
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  # Audience of the Scheduler's flush token (grafana.tf, local.flush_audience): a fixed name instead of the URL.
  custom_audiences = [local.flush_audience]

  # Service-level cap (the run.googleapis.com/maxScale annotation): the revision's scaling below does not set it,
  # so without this a console or gcloud edit could raise the service cap away from the revision cap. Stated here
  # so the two always agree (both var.max_instances) and a plan shows any drift.
  scaling {
    max_instance_count = var.max_instances
  }

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

      # S3 mode (ADR 0025): all four together or none. The bucket and role come from the AWS stack's state.
      dynamic "env" {
        for_each = local.s3_env
        content {
          name  = env.key
          value = env.value
        }
      }

      # Grafana Cloud export (ADR 0027, grafana.tf): none of these exist unless var.grafana_export is true.
      # The flush audience and identity are plain, non-sensitive values. The broker builds the Authorization
      # header at startup.
      dynamic "env" {
        for_each = local.grafana_env_names
        content {
          name  = env.key
          value = local.grafana_env[env.key]
        }
      }
      # The endpoint and the instance ID are secret references pinned to the Terraform-managed versions
      # (grafana.tf): no sensitive value sits in the env set, so the plan shows every entry.
      dynamic "env" {
        for_each = var.grafana_export ? [1] : []
        content {
          name = "OTEL_EXPORTER_OTLP_ENDPOINT"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.grafana_otlp_endpoint[0].secret_id
              version = google_secret_manager_secret_version.grafana_otlp_endpoint[0].version
            }
          }
        }
      }
      dynamic "env" {
        for_each = var.grafana_export ? [1] : []
        content {
          name = "FLEET_GRAFANA_INSTANCE_ID"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.grafana_instance_id[0].secret_id
              version = google_secret_manager_secret_version.grafana_instance_id[0].version
            }
          }
        }
      }
      dynamic "env" {
        for_each = var.grafana_export ? [1] : []
        content {
          name = "FLEET_GRAFANA_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.grafana_token_broker.secret_id
              version = "latest"
            }
          }
        }
      }

      # Only during a token rotation (README): the broker also accepts the previous tokens.
      dynamic "env" {
        for_each = var.rotation_in_progress ? {
          FLEET_WORKER_TOKEN_PREVIOUS = "fleet-worker-token-previous"
          FLEET_ADMIN_TOKEN_PREVIOUS  = "fleet-admin-token-previous"
        } : {}
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.token[env.value].secret_id
              version = "latest"
            }
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
        "--max-sigterm-delay=10s", # on SIGTERM, wait up to 10 s for open connections to finish
        "--structured-logs",       # JSON lines Cloud Logging parses
        "--http-address=0.0.0.0",  # so Cloud Run's probe can reach it
        "--http-port=9090",
        "--port=5432",
        google_sql_database_instance.fleet.connection_name,
      ]

      resources {
        limits = {
          # Cloud Run accepts whole CPUs above 1, so a total of 1.25 would likely be refused: both containers get 1.
          # Napkin math: billed only while a request is handled (about 43k active seconds a month: 0.43M requests
          # x 0.1 s) at 2 vCPU = about 86k vCPU-s of the 180k free. 0.75 broker + 0.25 proxy is untried.
          cpu    = "1"
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

  lifecycle {
    precondition {
      condition     = !var.grafana_export || (var.grafana_otlp_endpoint != null && var.grafana_instance_id != null)
      error_message = "grafana_export needs TF_VAR_grafana_otlp_endpoint and TF_VAR_grafana_instance_id (README, Grafana Cloud export)."
    }
  }

  depends_on = [
    google_project_service.api,
    google_project_iam_member.sql_client,
    google_project_iam_member.sql_instance_user,
    google_secret_manager_secret_iam_member.broker_reads,
    google_secret_manager_secret_iam_member.broker_reads_grafana,
    google_secret_manager_secret_iam_member.broker_reads_grafana_otlp_endpoint,
    google_secret_manager_secret_iam_member.broker_reads_grafana_instance_id,
    google_sql_user.broker,
  ]
}

# allUsers may call the URL; the application checks the bearer token (Q39). An organization policy that
# forbids allUsers would fail this apply: say so rather than weaken it.
#
# WARNING (kill switch, killswitch.tf, ADR 0024): ANY apply re-adds this binding, so it also undoes a fired
# switch. A plan showing this binding "to add" when you did not intend a reset means the switch fired: stop and
# investigate first (the check "broker_is_public" below also warns).
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.broker_image == null ? 0 : 1

  name     = google_cloud_run_v2_service.broker[0].name
  location = google_cloud_run_v2_service.broker[0].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Plan-time warning (not an error) when the deployed broker is NOT public: the kill switch has fired, or someone
# removed the binding. Check-scoped data sources report read failures as warnings too, so this is quiet before
# the broker exists. It reads the real policy each plan, which is what a plan diff can't show before refresh.
check "broker_is_public" {
  data "google_cloud_run_v2_service_iam_policy" "broker" {
    name     = "fleet-broker"
    location = var.region
  }

  assert {
    # Parsed, not searched: "allUsers" must be a member of the roles/run.invoker binding itself.
    condition = anytrue([
      for b in try(jsondecode(data.google_cloud_run_v2_service_iam_policy.broker.policy_data).bindings, []) :
      b.role == "roles/run.invoker" && contains(try(b.members, []), "allUsers")
    ])
    error_message = "The broker is not public: the budget kill switch has probably fired (ADR 0024). Investigate before applying: apply restores public access."
  }
}
