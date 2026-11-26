# APIs, the registry, the broker's identity and its secrets. The database is in cloudsql.tf, the service in
# cloudrun.tf (ADR 0021).

locals {
  apis = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
  ])

  # The IAM database user is the service account's email without ".gserviceaccount.com" (Cloud SQL's rule).
  db_user = trimsuffix(google_service_account.broker.email, ".gserviceaccount.com")

  registry = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.fleet.repository_id}"
}

resource "google_project_service" "api" {
  for_each = local.apis

  service = each.key
  # Destroying this stack must not switch APIs off: other things in the project may use them.
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "fleet" {
  repository_id = "fleet"
  location      = var.region
  format        = "DOCKER"
  description   = "Fleet container images"

  # The free tier is 0.5 GB and the broker image is about 230 MB: keep two, drop forgotten untagged layers.
  # A Cloud Run revision pinned to an older digest still runs, but a new instance of it could not pull once
  # it is cleaned up: deploy the digest you just pushed.
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-two-most-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }

  cleanup_policies {
    id     = "delete-untagged-after-7-days"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  depends_on = [google_project_service.api]
}

resource "google_service_account" "broker" {
  account_id   = "fleet-broker"
  display_name = "Fleet broker (Cloud Run)"

  depends_on = [google_project_service.api]
}

# Cloud SQL roles are granted on the project: Cloud SQL has no per-instance IAM binding for them. An IAM
# condition on the instance's resource name might narrow it; not tried, so this is the narrowest known form.
# The service account's only other permissions are the two secrets below.
resource "google_project_iam_member" "sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client" # connect through the Auth Proxy
  member  = "serviceAccount:${google_service_account.broker.email}"
}

resource "google_project_iam_member" "sql_instance_user" {
  project = var.project_id
  role    = "roles/cloudsql.instanceUser" # log in with IAM database authentication
  member  = "serviceAccount:${google_service_account.broker.email}"
}

# Containers only: the maintainer adds the values with gcloud (Q59), so no secret ever enters Terraform state.
locals {
  secret_ids = toset(["fleet-worker-token", "fleet-admin-token"])
}

resource "google_secret_manager_secret" "token" {
  for_each = local.secret_ids

  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.api]
}

# Per secret, not per project: the broker can read these two and nothing else in Secret Manager.
resource "google_secret_manager_secret_iam_member" "broker_reads" {
  for_each = google_secret_manager_secret.token

  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.broker.email}"
}
