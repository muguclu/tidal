output "id" {
  description = "Resource ID of the container registry; T-403 scopes AcrPull (kubelet, app identities) and T-306 scopes AcrPush (CI) to it per SPEC-06 section 2."
  value       = azurerm_container_registry.this.id
}

output "name" {
  description = "Registry name."
  value       = azurerm_container_registry.this.name
}

output "login_server" {
  description = "Registry login server (<name>.azurecr.io); used by images.yml and the Helm image repository."
  value       = azurerm_container_registry.this.login_server
}

output "purge_task_id" {
  description = "Resource ID of the weekly purge ACR task."
  value       = azurerm_container_registry_task.purge.id
}
