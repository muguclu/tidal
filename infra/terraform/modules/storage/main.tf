locals {
  tags = merge(var.tags, { component = var.component })
}

resource "azurerm_storage_account" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location

  # Checkov skips (each justified):
  #checkov:skip=CKV_AZURE_206:LRS is the SPEC-07 dev SKU; geo-redundancy would multiply at-rest cost for re-creatable data
  #checkov:skip=CKV_AZURE_59:Public endpoint stays on while enable_private_networking is false (SPEC-06 section 4); Entra ID + RBAC only, no shared key, no anonymous access
  #checkov:skip=CKV2_AZURE_1:No customer-managed key; Microsoft-managed keys suffice for non-PII market data
  #checkov:skip=CKV2_AZURE_33:No private endpoint while the private networking flag is off (Phase 4)
  #checkov:skip=CKV_AZURE_33:Queue service is not used
  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = "LRS"
  access_tier              = "Hot"

  # Flat namespace: blob index tags drive lifecycle (ADR-0004, storage-layout section 1).
  is_hns_enabled = false

  # Identity-based access only. The azurerm provider in the env composition MUST set
  # storage_use_azuread = true: containers go through ARM (storage_account_id) in azurerm 4.x, but the
  # account resource's own data-plane reads need Entra auth once shared key is off.
  shared_access_key_enabled         = false
  default_to_oauth_authentication   = true
  min_tls_version                   = "TLS1_2"
  https_traffic_only_enabled        = true
  allow_nested_items_to_be_public   = false
  cross_tenant_replication_enabled  = false
  local_user_enabled                = false
  infrastructure_encryption_enabled = true

  # Default (flag off): public endpoint enabled; RBAC + Entra-only auth apply, no keys, no anonymous access.
  # Flag on: public endpoint disabled and a private endpoint is expected (Phase 4).
  public_network_access_enabled = !var.enable_private_networking

  # Versioning stays off: Parquet files are immutable and restatements are new part files
  # (storage-layout section 2); versions would only add stored bytes. Soft delete covers accidents.
  blob_properties {
    versioning_enabled = false

    delete_retention_policy {
      days = var.soft_delete_days
    }

    container_delete_retention_policy {
      days = var.soft_delete_days
    }
  }

  tags = local.tags

}

resource "azurerm_storage_container" "this" {
  for_each = var.containers

  name                  = each.key
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"

  #checkov:skip=CKV2_AZURE_21:Blob read logging via diagnostic settings arrives in T-506
}
