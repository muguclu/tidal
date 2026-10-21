output "id" {
  description = "Storage account resource ID."
  value       = azurerm_storage_account.this.id
}

output "name" {
  description = "Storage account name."
  value       = azurerm_storage_account.this.name
}

output "primary_blob_endpoint" {
  description = "Primary blob service endpoint (https://<name>.blob.core.windows.net/)."
  value       = azurerm_storage_account.this.primary_blob_endpoint
}

output "container_ids" {
  description = "Map of container name to Resource Manager container ID. Scope RBAC role assignments (T-403) to these IDs (SPEC-06 section 2)."
  value       = { for k, c in azurerm_storage_container.this : k => c.id }
}

output "container_names" {
  description = "Sorted list of container names, for lifecycle rule prefix filters (T-207: prefix_match entries are <container>/...)."
  value       = sort(keys(azurerm_storage_container.this))
}
