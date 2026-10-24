terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

variable "subscription_id" {
  description = "Azure subscription GUID (dummy value is fine for validate)."
  type        = string
  default     = "00000000-0000-0000-0000-000000000000"
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

module "resource_group" {
  source = "../../../resource_group"

  env   = "dev"
  owner = "platform-team"
}

module "log_analytics" {
  source = "../../../log_analytics"

  env                 = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "observability"
}

module "budget" {
  source = "../../../budget"

  env                 = "dev"
  resource_group_name = module.resource_group.name
  resource_group_id   = module.resource_group.id
  tags                = module.resource_group.tags
  component           = "finops"
  alert_emails        = ["finops@example.invalid"]
  start_date          = "2026-11-01T00:00:00Z"
}

module "monitoring" {
  source = "../.."

  env                 = "dev"
  resource_group_name = module.resource_group.name
  resource_group_id   = module.resource_group.id
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "observability"

  action_group_id = module.budget.action_group_id
  app_insights_id = module.log_analytics.app_insights_id
  workspace_id    = module.log_analytics.workspace_id

  # Dummy IDs: validate and checkov do not resolve them.
  servicebus_namespace_id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.ServiceBus/namespaces/sb-example"
  storage_account_id                = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-tidal-dev/providers/Microsoft.Storage/storageAccounts/stexample"
  expected_blob_modifier_object_ids = ["11111111-1111-1111-1111-111111111111"]
}

output "workbook_id" {
  description = "Workbook resource ID."
  value       = module.monitoring.workbook_id
}
