# No secret values here, and none exist in state: the tokens are added with gcloud.
output "service_url" {
  description = "The broker's public URL (null until broker_image is set)."
  value       = one(google_cloud_run_v2_service.broker[*].uri)
}

output "instance_connection_name" {
  description = "PROJECT:REGION:INSTANCE, for cloud-sql-proxy."
  value       = google_sql_database_instance.fleet.connection_name
}

output "registry_path" {
  description = "Where `docker push` goes: <this>/broker@... or <this>/broker:tag."
  value       = local.registry
}

output "iam_database_user" {
  description = "The Cloud SQL user the broker logs in as (the GRANT in the README names it)."
  value       = local.db_user
}
