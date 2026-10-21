resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-tidal-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = var.retention_in_days
  daily_quota_gb      = var.daily_quota_gb

  tags = var.tags
}

# Workspace-based Application Insights (classic standalone components are retired).
# Sampling: service-side ingestion sampling is set via sampling_percentage (default 100 = keep all).
# The primary lever is SDK-side sampling in the OpenTelemetry exporter; the workspace daily cap
# above is the hard cost ceiling, not a sampling strategy.
resource "azurerm_application_insights" "this" {
  name                = "appi-tidal-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  workspace_id        = azurerm_log_analytics_workspace.this.id
  application_type    = "web"
  retention_in_days   = var.retention_in_days
  sampling_percentage = var.sampling_percentage

  # Kept enabled: workload identities hold no Monitoring Metrics Publisher role (SPEC-06 section 2),
  # so connection-string ingestion must keep working. Revisit with T-403.
  local_authentication_enabled = true

  tags = var.tags
}
