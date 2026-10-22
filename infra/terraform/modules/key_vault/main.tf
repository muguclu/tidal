data "azurerm_client_config" "current" {}

locals {
  tags = merge(var.tags, { component = var.component })
}

resource "azurerm_key_vault" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # Checkov skips (each justified):
  #checkov:skip=CKV2_AZURE_32:No private endpoint while the private networking flag is off (Phase 4)
  #checkov:skip=CKV_AZURE_189:Public endpoint stays on while enable_private_networking is false (SPEC-06 section 4); Entra ID + RBAC only
  #checkov:skip=CKV_AZURE_109:Default Deny applies when enable_private_networking is true; Allow in dev so the operator can reach the vault

  # RBAC mode: no access policies. Role assignments are made in T-403, per secret where possible.
  rbac_authorization_enabled = true

  purge_protection_enabled   = true
  soft_delete_retention_days = var.soft_delete_retention_days

  # Default (flag off): public endpoint enabled; Entra ID + RBAC apply.
  # Flag on: public access disabled (private endpoint in Phase 4).
  public_network_access_enabled = !var.enable_private_networking

  # Always emitted: Allow while the flag is off (public endpoint, RBAC only); Deny when on.
  network_acls {
    default_action = var.enable_private_networking ? "Deny" : "Allow"
    bypass         = "AzureServices"
  }

  tags = local.tags
}
