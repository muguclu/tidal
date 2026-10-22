output "action_group_id" {
  description = "Action group resource ID; reused by the T-506 platform alerts."
  value       = azurerm_monitor_action_group.this.id
}

output "action_group_name" {
  description = "Action group name (ag-tidal-<env>)."
  value       = azurerm_monitor_action_group.this.name
}

output "budget_id" {
  description = "Budget resource ID (resource group or subscription scope, per budget_scope)."
  value       = one(concat(azurerm_consumption_budget_resource_group.this[*].id, azurerm_consumption_budget_subscription.this[*].id))
}

output "anomaly_alert_id" {
  description = "Cost anomaly alert ID, or null when enable_anomaly_alert is false."
  value       = one(azurerm_cost_anomaly_alert.this[*].id)
}

output "credit_budget_ids" {
  description = "Map of credit alert amount to budget ID (empty unless credit_alert_amounts is set)."
  value       = { for k, b in azurerm_consumption_budget_subscription.credit : k => b.id }
}
