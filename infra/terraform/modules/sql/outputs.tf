output "server_id" {
  description = "SQL server resource ID."
  value       = azurerm_mssql_server.this.id
}

output "server_name" {
  description = "SQL server name."
  value       = azurerm_mssql_server.this.name
}

output "server_fqdn" {
  description = "SQL server FQDN. Maps to config-contract TIDAL_SQL__SERVER."
  value       = azurerm_mssql_server.this.fully_qualified_domain_name
}

output "database_id" {
  description = "Database resource ID."
  value       = azurerm_mssql_database.tidal.id
}

output "database_name" {
  description = "Database name. Maps to config-contract TIDAL_SQL__DATABASE."
  value       = azurerm_mssql_database.tidal.name
}

output "server_principal_id" {
  description = "Principal ID of the server system-assigned identity, or null when disabled."
  value       = try(azurerm_mssql_server.this.identity[0].principal_id, null)
}

output "auditing_enabled" {
  description = "True when server auditing to Log Analytics is configured."
  value       = local.auditing_enabled
}

output "firewall_rule_names" {
  description = "Names of the single-IP firewall rules created from allowed_ip_addresses (empty with private networking)."
  value       = sort([for r in azurerm_mssql_firewall_rule.allowed_ip : r.name])
}
