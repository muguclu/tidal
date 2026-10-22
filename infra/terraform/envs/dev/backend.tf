# Partial backend configuration: resource group, storage account, container and key come from
# `terraform init -backend-config=backend.hcl` (see backend.hcl.example). Nothing is hard-coded.
# Entra ID authentication only; the state account has shared key access disabled.
terraform {
  backend "azurerm" {
    use_azuread_auth = true
  }
}
