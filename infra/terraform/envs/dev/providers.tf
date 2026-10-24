provider "azurerm" {
  subscription_id = var.subscription_id

  # Shared key access is disabled on every storage account (SPEC-06 section 4), so the provider
  # must use Entra ID for blob/container data-plane calls.
  storage_use_azuread = true

  features {
    key_vault {
      # Purge protection is on, so a soft-deleted vault cannot be purged anyway. Recover a vault of the
      # same name instead of failing when an environment is rebuilt inside the retention window.
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }

    storage {
      # Skip data-plane calls (blob/queue service properties) during refresh and plan. With enable_private_networking
      # the storage account has public access off and a runner or laptop outside the VNet would time out reading them.
      # Changes: the provider no longer reads or manages blob service properties from the data plane; containers and
      # the account itself still go through ARM. Harmless with the flag off, so it is set unconditionally.
      data_plane_available = false
    }

    resource_group {
      # `terraform destroy` must remove the whole environment even when the group still holds resources
      # that Terraform does not track (for example items created by the platform itself).
      prevent_deletion_if_contains_resources = false
    }
  }
}

provider "azapi" {
  subscription_id = var.subscription_id
}
