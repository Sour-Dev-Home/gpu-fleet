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
    "cloudresourcemanager.googleapis.com", # project IAM bindings go through this API
    "monitoring.googleapis.com",           # the 4xx alert (usually on by default; explicit is safer)
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

  # The free tier is 0.5 GB and the broker image is about 230 MB (layers are shared between versions).
  # Every build gets a unique tag (the short git SHA), never :latest, and tags can't be moved (immutable).
  # So the untagged-cleanup rule below only ever removes leftovers; tagged versions are never deleted by it,
  # and the keep rule protects the two newest. Old tagged images pile up: delete them by hand when the
  # registry nears 0.5 GB (`gcloud artifacts docker images delete`).
  cleanup_policy_dry_run = false

  docker_config {
    immutable_tags = true
  }

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

# Cloud SQL roles are granted on the project (Cloud SQL has no per-instance binding), narrowed by an IAM
# condition to this one instance. UNVERIFIED that the API accepts conditions on both roles: `validate` only
# checks the schema, so a refusal would show at apply. The service account's only other permissions are the
# secrets below.
resource "google_project_iam_member" "sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client" # connect through the Auth Proxy
  member  = "serviceAccount:${google_service_account.broker.email}"

  condition {
    title       = "fleet-instance-only"
    description = "Only the fleet Cloud SQL instance"
    expression  = "resource.name == \"projects/${var.project_id}/instances/${google_sql_database_instance.fleet.name}\" && resource.type == \"sqladmin.googleapis.com/Instance\""
  }

  depends_on = [google_project_service.api]
}

resource "google_project_iam_member" "sql_instance_user" {
  project = var.project_id
  role    = "roles/cloudsql.instanceUser" # log in with IAM database authentication
  member  = "serviceAccount:${google_service_account.broker.email}"

  condition {
    title       = "fleet-instance-only"
    description = "Only the fleet Cloud SQL instance"
    expression  = "resource.name == \"projects/${var.project_id}/instances/${google_sql_database_instance.fleet.name}\" && resource.type == \"sqladmin.googleapis.com/Instance\""
  }

  depends_on = [google_project_service.api]
}

# Containers only: the maintainer adds the values with gcloud (Q59), so no secret ever enters Terraform state.
# The two "-previous" secrets exist only during a token rotation (var.rotation_in_progress; README).
locals {
  secret_ids = toset(concat(
    ["fleet-worker-token", "fleet-admin-token"],
    var.rotation_in_progress ? ["fleet-worker-token-previous", "fleet-admin-token-previous"] : [],
  ))
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
