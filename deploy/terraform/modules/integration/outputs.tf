output "api_uri" {
  value       = google_cloud_run_v2_service.service["api"].uri
  description = "Cloud Run URL of the API service (map public_base_url's domain to it)."
}

output "service_account" {
  value = google_service_account.app.email
}
