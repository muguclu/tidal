output "id" {
  description = "Resource group resource ID."
  value       = azurerm_resource_group.this.id
}

output "name" {
  description = "Resource group name (rg-tidal-<env>)."
  value       = azurerm_resource_group.this.name
}

output "location" {
  description = "Resource group region."
  value       = azurerm_resource_group.this.location
}

output "tags" {
  description = "Merged tag map (mandatory plus extra). Pass to sibling modules and override component per resource."
  value       = local.tags
}
