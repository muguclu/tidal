# Server auditing to Log Analytics (T-407, SPEC-06) and the optional Defender for SQL / Vulnerability Assessment
# (ADR-0017). Auditing is on by default; Defender and VA are off by default (cost, see docs/finops/deltas/T-407.md).

locals {
  auditing_enabled = var.log_analytics_workspace_id != null
  defender_enabled = var.enable_defender_for_sql
}

# Server-level audit policy. log_monitoring_enabled routes events to the diagnostic setting below.
# No storage account or shared key is used (storage sink needs the server identity and costs extra), so the
# retention is the workspace retention (30 days, SPEC-07 section 2); retention_in_days only applies to a storage sink.
resource "azurerm_mssql_server_extended_auditing_policy" "this" {
  count = local.auditing_enabled ? 1 : 0

  server_id              = azurerm_mssql_server.this.id
  log_monitoring_enabled = true
  enabled                = true

  # A storage sink is deliberately not configured; log_monitoring_enabled is the only destination.
}

# The azurerm policy has no action-group argument, so the groups are patched onto the same audit setting with azapi
# (provider already present for ADR-0011). Dev default is a low-volume set (var.audit_action_groups): failed logins and
# security-relevant changes, not every batch. If azurerm adds the argument, move it there and delete this resource.
resource "azapi_update_resource" "audit_action_groups" {
  count = local.auditing_enabled ? 1 : 0

  type        = "Microsoft.Sql/servers/auditingSettings@2023-08-01"
  resource_id = "${azurerm_mssql_server.this.id}/auditingSettings/default"

  body = {
    properties = {
      state                       = "Enabled"
      isAzureMonitorTargetEnabled = true
      auditActionsAndGroups       = var.audit_action_groups
    }
  }

  depends_on = [azurerm_mssql_server_extended_auditing_policy.this]
}

# Server auditing to Log Analytics is delivered through a diagnostic setting on the master database
# (documented Azure pattern).
resource "azurerm_monitor_diagnostic_setting" "sql_audit" {
  count = local.auditing_enabled ? 1 : 0

  name                       = "diag-audit-${local.server_name}"
  target_resource_id         = "${azurerm_mssql_server.this.id}/databases/master"
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category = "SQLSecurityAuditEvents"
  }

  # The auditing policy must exist first, otherwise the category is accepted but no events are produced.
  depends_on = [azurerm_mssql_server_extended_auditing_policy.this]
}

# Defender for SQL (advanced threat protection) on this server only, not the subscription-wide plan.
# Billed per server (meter "Standard Node" 13.1996 EUR/month in westeurope) regardless of auto-pause.
resource "azurerm_mssql_server_security_alert_policy" "this" {
  count = local.defender_enabled ? 1 : 0

  resource_group_name = var.resource_group_name
  server_name         = azurerm_mssql_server.this.name
  state               = "Enabled"
  retention_days      = 0
}

# Vulnerability Assessment with the Express configuration: no storage account and no SAS key, results are kept
# by the service. Requires Defender for SQL on the server.
resource "azapi_resource" "va_express" {
  count = local.defender_enabled && var.enable_vulnerability_assessment ? 1 : 0

  type      = "Microsoft.Sql/servers/sqlVulnerabilityAssessments@2023-08-01"
  name      = "default"
  parent_id = azurerm_mssql_server.this.id

  body = {
    properties = {
      state = "Enabled"
    }
  }

  depends_on = [azurerm_mssql_server_security_alert_policy.this]
}
