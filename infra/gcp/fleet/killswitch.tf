# Budget kill switch (ADR 0024, Q70-Q73). When this project's spend reaches the budget, public access to the
# broker is removed: `allUsers` leaves roles/run.invoker on the broker service, so callers get Google's 403.
#
#   Cloud Billing budget -> Pub/Sub topic -> push subscription (OIDC) -> private Cloud Run service
#   `fleet-killswitch` -> removes allUsers from the broker's invoker role.
#
# RESET: `terraform apply`. The broker's allUsers binding (google_cloud_run_v2_service_iam_member.public in
# cloudrun.tf) is a Terraform resource, so apply re-adds it. A plan that shows that binding "to add" means the
# switch has fired: fix the cause (and raise var.budget_usd if the spend was legitimate) before applying.

data "google_project" "this" {
  project_id = var.project_id
  # No depends_on on the API set: cloudresourcemanager is already enabled, and a dependency would defer the
  # read, leaving the project number unknown at plan time.
}

locals {
  # Cloud Run's resource name for the broker; built from names, not from the service, so it also exists
  # in the plan while the broker is not deployed.
  broker_resource = "projects/${var.project_id}/locations/${var.region}/services/fleet-broker"

  # Both services must exist for the switch to have anything to do or anything to act on.
  killswitch_enabled = var.killswitch_image != null && var.broker_image != null
}

# --- identities -----------------------------------------------------------------------------------------------

resource "google_service_account" "killswitch" {
  account_id   = "fleet-killswitch"
  display_name = "Fleet kill switch (Cloud Run runtime)"

  depends_on = [google_project_service.api]
}

resource "google_service_account" "budget_pusher" {
  account_id   = "fleet-budget-pusher"
  display_name = "Fleet budget push subscription (calls the kill switch)"

  depends_on = [google_project_service.api]
}

# The two permissions the switch needs, and nothing else. roles/run.admin is the only predefined role with
# run.services.setIamPolicy, and it can also deploy and delete services. Whether the API accepts a custom role
# in a per-service binding is UNVERIFIED until the first apply.
resource "google_project_iam_custom_role" "killswitch" {
  role_id     = "fleetKillSwitch"
  title       = "Fleet kill switch"
  description = "Read and write the IAM policy of a Cloud Run service. Bound on the broker service only."
  permissions = [
    "run.services.getIamPolicy",
    "run.services.setIamPolicy",
  ]
}

# Bound on the broker SERVICE only (not the project): the switch can edit who may call the broker and
# nothing else.
resource "google_cloud_run_v2_service_iam_member" "killswitch_edits_broker" {
  count = local.killswitch_enabled ? 1 : 0

  name     = google_cloud_run_v2_service.broker[0].name
  location = google_cloud_run_v2_service.broker[0].location
  role     = google_project_iam_custom_role.killswitch.name
  member   = "serviceAccount:${google_service_account.killswitch.email}"
}

# --- the kill switch service ----------------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "killswitch" {
  count = var.killswitch_image == null ? 0 : 1

  name     = "fleet-killswitch"
  location = var.region

  # Pub/Sub's push comes from Google's network, so ingress is open; there is NO allUsers binding, so only a
  # caller with an invoker token (the push subscription's service account) gets in.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account       = google_service_account.killswitch.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN1"
    timeout               = "60s" # matches the subscription's ack deadline

    scaling {
      min_instance_count = 0
      max_instance_count = 1 # a few messages a day; one instance also serialises the IAM edits
    }

    containers {
      name  = "killswitch"
      image = var.killswitch_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi" # the Google client libraries load gRPC: 256 Mi is untried
        }
        cpu_idle = true # request-based billing
      }

      env {
        name  = "FLEET_BROKER_SERVICE"
        value = local.broker_resource
      }
    }
  }

  depends_on = [google_project_service.api]
}

# Only the push subscription's account may call the switch. Never allUsers.
resource "google_cloud_run_v2_service_iam_member" "pusher_invokes" {
  count = var.killswitch_image == null ? 0 : 1

  name     = google_cloud_run_v2_service.killswitch[0].name
  location = google_cloud_run_v2_service.killswitch[0].location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.budget_pusher.email}"
}

# --- Pub/Sub --------------------------------------------------------------------------------------------------

resource "google_pubsub_topic" "budget" {
  name = "fleet-budget"

  depends_on = [google_project_service.api]
}

# Cloud Billing must be allowed to publish. The docs say the granter needs permission to do this but do not name
# the account: billing-budget-alert@system.gserviceaccount.com is UNVERIFIED. If the apply refuses the member, or
# the drill works but a real notification never arrives, check Cloud Billing's budget-notification docs.
resource "google_pubsub_topic_iam_member" "billing_publishes" {
  topic  = google_pubsub_topic.budget.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:billing-budget-alert@system.gserviceaccount.com"
}

# Needed in some setups (UNVERIFIED): the Pub/Sub service agent mints the OIDC token as the pusher account.
# Projects created after April 2021 don't need the grant, but it is harmless. Granted on that one account, not
# the project. It waits for the topic, so the Pub/Sub API is on and the service agent exists.
resource "google_service_account_iam_member" "pubsub_mints_pusher_token" {
  service_account_id = google_service_account.budget_pusher.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  depends_on = [google_project_service.api, google_pubsub_topic.budget]
}

# No dead-letter topic: a message the switch cannot handle is acknowledged (malformed) or retried with backoff
# (API error), and the next budget notification, several a day, carries fresh numbers.
resource "google_pubsub_subscription" "killswitch" {
  count = var.killswitch_image == null ? 0 : 1

  name  = "fleet-budget-killswitch"
  topic = google_pubsub_topic.budget.id

  ack_deadline_seconds       = 60
  message_retention_duration = "259200s" # 3 days
  expiration_policy {
    ttl = "" # never expire (the default deletes a subscription after 31 days without activity)
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  push_config {
    push_endpoint = google_cloud_run_v2_service.killswitch[0].uri

    oidc_token {
      service_account_email = google_service_account.budget_pusher.email
      audience              = google_cloud_run_v2_service.killswitch[0].uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.pusher_invokes,
    google_service_account_iam_member.pubsub_mints_pusher_token,
  ]
}

# --- the budget -----------------------------------------------------------------------------------------------

# Needs the provider's billing_project and user_project_override (main.tf) with user credentials.
# Budget data lags by hours (ADR 0024): this caps the damage, not the first dollar.
resource "google_billing_budget" "fleet" {
  billing_account = var.billing_account
  display_name    = "fleet project kill switch"

  budget_filter {
    projects        = ["projects/${data.google_project.this.number}"] # this project only, by NUMBER
    calendar_period = "MONTH"
    # Gross usage: credits do not hide the spend (with ~$296 of credits a net figure would stay near $0).
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }

  # 50% and 90%: emails to the billing admins (default recipients). 100%: the same email, and the push below
  # carries every update, so the switch decides from the amounts, not from this rule.
  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  all_updates_rule {
    pubsub_topic                   = google_pubsub_topic.budget.id
    schema_version                 = "1.0"
    disable_default_iam_recipients = false
  }

  depends_on = [
    google_project_service.api,
    google_pubsub_topic_iam_member.billing_publishes,
  ]
}
