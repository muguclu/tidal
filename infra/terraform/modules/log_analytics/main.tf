locals {
  tags = merge(var.tags, { component = var.component })
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-tidal-${var.env}"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = var.retention_in_days
  daily_quota_gb      = var.daily_quota_gb

  # Local (shared key) auth off: access is Entra only.
  local_authentication_enabled = false

  # Public endpoints stay on. Azure Monitor Private Link Scope (AMPLS) is skipped for cost reasons;
  # the private networking flag is Phase 4.
  internet_ingestion_enabled = true
  internet_query_enabled     = true

  tags = local.tags
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

  # Entra only (T-506, ADR-0008 R-24/R-27): ingestion without a token is rejected, so the connection string alone is
  # not a credential. The telemetry identities hold Monitoring Metrics Publisher on this resource (envs/dev/rbac.tf).
  local_authentication_enabled = false

  tags = local.tags
}
