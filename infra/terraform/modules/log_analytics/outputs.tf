output "workspace_id" {
  description = "Log Analytics workspace resource ID."
  value       = azurerm_log_analytics_workspace.this.id
}

output "workspace_name" {
  description = "Log Analytics workspace name."
  value       = azurerm_log_analytics_workspace.this.name
}

output "workspace_customer_id" {
  description = "Log Analytics workspace (customer) ID GUID, used by agents and queries."
  value       = azurerm_log_analytics_workspace.this.workspace_id
}

output "app_insights_id" {
  description = "Application Insights resource ID."
  value       = azurerm_application_insights.this.id
}

output "app_insights_connection_string" {
  description = "Application Insights connection string (APPLICATIONINSIGHTS_CONNECTION_STRING). Sensitive; store in Key Vault, not in config."
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}
