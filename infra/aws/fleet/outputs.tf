output "results_bucket" {
  description = "Where workers upload results through presigned URLs (phase 4)."
  value       = module.results.name
}
