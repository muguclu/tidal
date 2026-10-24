output "environment_id" {
  description = "Resource ID of the Container Apps environment."
  value       = azurerm_container_app_environment.this.id
}

output "query_app_name" {
  description = "Name of the tidal-query Container App (park script and deploy workflow target)."
  value       = azurerm_container_app.query.name
}

output "query_app_id" {
  description = "Resource ID of the tidal-query Container App (target of the ingress IP allow-list patch in envs/dev/apps.tf)."
  value       = azurerm_container_app.query.id
}

output "query_fqdn" {
  description = "Ingress FQDN of tidal-query; maps to TIDAL_QUERY__BASE_URL as https://<fqdn>. null when the app has no ingress."
  value       = one(azurerm_container_app.query.ingress[*].fqdn)
}

output "job_names" {
  description = "Map of job short name to the Container Apps Job resource name."
  value       = { for k, j in azurerm_container_app_job.this : k => j.name }
}

output "job_ids" {
  description = "Map of job short name to resource ID."
  value       = { for k, j in azurerm_container_app_job.this : k => j.id }
}

output "default_domain" {
  description = "Default domain of the environment (for the private DNS zone of an internal environment, network module)."
  value       = azurerm_container_app_environment.this.default_domain
}

output "static_ip_address" {
  description = "Static IP of the environment (internal load balancer address when internal_load_balancer_enabled is true)."
  value       = azurerm_container_app_environment.this.static_ip_address
}
