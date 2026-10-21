output "tenant_id" {
  description = "Entra tenant ID (GitHub variable AZURE_TENANT_ID)."
  value       = data.azuread_client_config.current.tenant_id
}

output "subscription_id" {
  description = "Subscription ID (GitHub variable AZURE_SUBSCRIPTION_ID)."
  value       = var.subscription_id
}

output "ci_client_id" {
  description = "Application (client) ID of sp-tidal-ci (GitHub variable AZURE_CLIENT_ID). Not a secret."
  value       = azuread_application.ci.client_id
}

output "ci_service_principal_object_id" {
  description = "Object ID of the sp-tidal-ci service principal (for role assignments in later tasks)."
  value       = azuread_service_principal.ci.object_id
}

output "state_resource_group_name" {
  description = "Resource group holding the state account."
  value       = azurerm_resource_group.tfstate.name
}

output "state_storage_account_name" {
  description = "Name of the Terraform state storage account."
  value       = azurerm_storage_account.tfstate.name
}

output "state_container_name" {
  description = "Name of the Terraform state container."
  value       = azurerm_storage_container.tfstate.name
}

output "backend_config" {
  description = "Backend block to paste into infra/terraform/envs/<env>/backend.tf (Entra auth, no keys)."
  value       = <<-EOT
    terraform {
      backend "azurerm" {
        resource_group_name  = "${azurerm_resource_group.tfstate.name}"
        storage_account_name = "${azurerm_storage_account.tfstate.name}"
        container_name       = "${azurerm_storage_container.tfstate.name}"
        key                  = "${var.env}.tfstate"
        use_azuread_auth     = true
      }
    }
  EOT
}
