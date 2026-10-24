locals {
  tags = merge(var.tags, { component = var.component })

  # Daily exports run for ten years unless the caller bounds them (same default window as the budget module).
  end_date = coalesce(var.recurrence_end_date, timeadd(var.recurrence_start_date, "87600h"))
}

# Reuses the environment storage account (no extra resource, no extra fixed cost): a dedicated private container
# keeps the export, the reader role and any later lifecycle rule separate from market data. The account has shared
# key access off, so the export writes through the managed identity that Cost Management creates for it.
resource "azurerm_storage_container" "this" {
  name                  = var.container_name
  storage_account_id    = var.storage_account_id
  container_access_type = "private"

  #checkov:skip=CKV2_AZURE_21:Blob read logging via diagnostic settings arrives in T-506
}

# Resource-group scope: needs no subscription-level rights. Cost data of other groups (notably the AKS node group
# MC_*) is NOT included; see README and the report limits. The export resource itself has no tags argument.
resource "azurerm_resource_group_cost_management_export" "this" {
  name                         = "export-tidal-${var.env}-actual"
  resource_group_id            = var.resource_group_id
  recurrence_type              = "Daily"
  recurrence_period_start_date = var.recurrence_start_date
  recurrence_period_end_date   = local.end_date

  export_data_storage_location {
    container_id     = azurerm_storage_container.this.id
    root_folder_path = var.root_folder_path
  }

  export_data_options {
    type       = "ActualCost"
    time_frame = "MonthToDate"
  }

  # Azure rejects start dates in the past on create and a change would recreate the export, so only the first
  # value counts.
  lifecycle {
    ignore_changes = [recurrence_period_start_date]
  }
}

# Read-only access for the CI identity, on the export container only (not the account): no market data, no
# state. Cost Management Reader would also expose live cost queries and is broader than a CSV download needs.
resource "azurerm_role_assignment" "reader" {
  count = var.reader_principal_id == null ? 0 : 1

  scope                = azurerm_storage_container.this.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = var.reader_principal_id
  principal_type       = "ServicePrincipal"
  description          = "T-502: sp-tidal-ci-plan reads the Cost Management export CSVs (weekly cost report)"
}
