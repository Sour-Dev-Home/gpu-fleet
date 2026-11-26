# The job store (ADR 0020, ADR 0021). A public IPv4 address, but nothing can use it without the Auth Proxy
# and an IAM login: no authorized networks, connectors required, encryption required (Q57).
#trivy:ignore:GCP-0017 (the public IP is the decision, Q57: no authorized network, connectors required, TLS only)
resource "google_sql_database_instance" "fleet" {
  name             = "fleet"
  region           = var.region
  database_version = "POSTGRES_18"

  # Terraform refuses to destroy it; the second flag below makes the API refuse too. Both off to tear down.
  deletion_protection = true

  # No root_password (Q58): the postgres password is set with gcloud and never enters state.

  settings {
    edition           = "ENTERPRISE"
    tier              = "db-f1-micro" # shared core, 0.6 GB: about $7.30/month (docs/research/phase-4.md)
    availability_type = "ZONAL"       # no standby: HA would double the price

    disk_type             = "PD_SSD"
    disk_size             = 10
    disk_autoresize       = true
    disk_autoresize_limit = 20 # growth stops at 20 GB, so a runaway table can't grow the bill without bound

    deletion_protection_enabled = true
    connector_enforcement       = "REQUIRED" # refuse any connection that doesn't come through a connector or the proxy

    backup_configuration {
      enabled = true
      # Point-in-time recovery stores the write-ahead log. PostgreSQL offers it on every tier as far as the
      # docs say; UNVERIFIED for db-f1-micro, so a failed apply here means: set this to false (backups only).
      point_in_time_recovery_enabled = true
      start_time                     = "07:00" # UTC, the quietest hour for the maintainer's time zone

      backup_retention_settings {
        retained_backups = 7
      }
    }

    maintenance_window {
      day          = 7                        # Sunday
      hour         = var.maintenance_utc_hour # Sunday 09:00 UTC by default (04:00 Eastern)
      update_track = "stable"
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    # Security logging. Napkin math: a few hundred connections a day (the pool opens at most 4 per instance and
    # recycles every 30 minutes) at about 200 bytes a line is under 15 MB a month, against 50 GiB free.
    # UNVERIFIED: PostgreSQL 18 changed log_connections from a bool to a list of options; if apply refuses
    # "on", the flag needs the value the Cloud SQL docs list for 18 (e.g. "authentication").
    database_flags {
      name  = "log_connections"
      value = "on"
    }
    database_flags {
      name  = "log_disconnections"
      value = "on"
    }
    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }
    database_flags {
      name  = "log_checkpoints"
      value = "on"
    }
    database_flags {
      name  = "log_temp_files"
      value = "0" # log every temporary file
    }

    ip_configuration {
      ipv4_enabled = true # the public IP; reachable only through the proxy, since no network is authorized
      ssl_mode     = "ENCRYPTED_ONLY"
      # No authorized_networks block: that list is how an address would be let in without a connector.
    }
  }

  depends_on = [google_project_service.api]
}

resource "google_sql_database" "fleet" {
  name     = "fleet"
  instance = google_sql_database_instance.fleet.name
}

# The broker logs in as its service account: no password exists. The user starts with no privileges; the
# one-time GRANT in the README gives it the database (Q58).
resource "google_sql_user" "broker" {
  name     = local.db_user
  instance = google_sql_database_instance.fleet.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}
