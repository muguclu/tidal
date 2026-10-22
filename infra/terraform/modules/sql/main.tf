locals {
  tags        = merge(var.tags, { component = var.component })
  server_name = "sql-tidal-${var.env}-${var.server_name_suffix}"

  # Public endpoint follows the Phase 4 private networking flag (SPEC-06 section 4).
  public_network_access_enabled = !var.enable_private_networking
}

# Entra-only: no administrator_login / administrator_login_password anywhere (SPEC-06 section 2).
# Workload identities are mapped in the database with CREATE USER [id-x] FROM EXTERNAL PROVIDER (T-106 migrations).
resource "azurerm_mssql_server" "this" {
  # checkov:skip=CKV_AZURE_113: public access follows enable_private_networking (SPEC-06 section 4); it is on until Phase 4 and access is Entra-only over TLS 1.2 with no firewall rules by default.
  # checkov:skip=CKV2_AZURE_45: the private endpoint belongs to the Phase 4 network module and is created when enable_private_networking is true.
  # checkov:skip=CKV_AZURE_23: server auditing needs a Log Analytics or storage sink; deferred to T-506 (security hardening).
  # checkov:skip=CKV_AZURE_24: audit retention is part of the same auditing configuration; deferred to T-506.
  # checkov:skip=CKV2_AZURE_2: vulnerability assessment needs auditing and a storage account (Defender for SQL cost); deferred to T-506.
  name                = local.server_name
  resource_group_name = var.resource_group_name
  location            = var.location
  version             = "12.0"

  minimum_tls_version           = "1.2"
  public_network_access_enabled = local.public_network_access_enabled

  azuread_administrator {
    login_username              = var.entra_admin_login
    object_id                   = var.entra_admin_object_id
    tenant_id                   = var.tenant_id
    azuread_authentication_only = true
  }

  dynamic "identity" {
    for_each = var.enable_system_assigned_identity ? [1] : []
    content {
      type = "SystemAssigned"
    }
  }

  tags = local.tags
}

resource "azurerm_mssql_database" "tidal" {
  # checkov:skip=CKV_AZURE_229: zone redundancy is deliberately off (SPEC-07 section 2, cost); backups are Local.
  # checkov:skip=CKV_AZURE_224: ledger is not required (no cryptographic non-repudiation requirement) and cannot be enabled later on an existing database.
  name      = var.database_name
  server_id = azurerm_mssql_server.this.id

  # GP serverless Gen5, max 2 vCore (the _2 suffix), SPEC-07 section 2.
  sku_name                    = var.sku_name
  min_capacity                = var.min_capacity
  auto_pause_delay_in_minutes = var.auto_pause_delay_in_minutes
  max_size_gb                 = var.max_size_gb

  zone_redundant       = false
  storage_account_type = "Local"
  geo_backup_enabled   = false

  tags = local.tags
}

# Optional "Allow Azure services" rule (0.0.0.0 to 0.0.0.0). It admits every Azure tenant's traffic to the
# public endpoint, so it is off by default and only created while public access is on.
resource "azurerm_mssql_firewall_rule" "allow_azure_services" {
  # checkov:skip=CKV2_AZURE_34: optional 0.0.0.0 rule, count = 0 by default (allow_azure_services = false); never a 0.0.0.0/0 range.
  count = var.allow_azure_services && local.public_network_access_enabled ? 1 : 0

  name             = "AllowAzureServices"
  server_id        = azurerm_mssql_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# Free offer (ADR-0011, LOCAL-DEV section 2): 100k vCore-s + 32 GB per month, AutoPause at the limit.
# azurerm 4.81.0 has no native arguments (upstream PRs 32055 / 32277 open), so azapi sets the properties.
# When azurerm ships use_free_limit / free_limit_exhaustion_behavior: set them on azurerm_mssql_database,
# remove this resource and the azapi provider, and `terraform state rm` the azapi resource (no recreate).
# API version 2025-01-01 (GA) carries useFreeLimit / freeLimitExhaustionBehavior in the azapi 2.13.0 embedded schema.
# Caveat: the offer is normally chosen at creation. Whether the API accepts it as an update on the just-created
# database is not verified (no Azure credentials in this task). If the first dev apply rejects it, replace
# azurerm_mssql_database with an azapi_resource carrying the same properties plus the SKU settings.
resource "azapi_update_resource" "free_offer" {
  count = var.use_free_offer ? 1 : 0

  type        = "Microsoft.Sql/servers/databases@2025-01-01"
  resource_id = azurerm_mssql_database.tidal.id

  body = {
    properties = {
      useFreeLimit                = true
      freeLimitExhaustionBehavior = "AutoPause"
    }
  }
}
