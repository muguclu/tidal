output "container_id" {
  description = "Resource Manager ID of the export container (scope for the reader role)."
  value       = azurerm_storage_container.this.id
}

output "container_name" {
  description = "Export container name; the workflow downloads from here."
  value       = azurerm_storage_container.this.name
}

output "export_id" {
  description = "Cost Management export resource ID."
  value       = azurerm_resource_group_cost_management_export.this.id
}

output "export_path_prefix" {
  description = "Blob prefix of the export runs: <root_folder_path>/<export name>/."
  value       = "${var.root_folder_path}/${azurerm_resource_group_cost_management_export.this.name}/"
}

output "tags" {
  description = "Effective tag map (component overridden), exposed for the env composition."
  value       = local.tags
}
