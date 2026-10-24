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

module "storage" {
  source = "../../../storage"

  name                = "sttidaldevexample"
  resource_group_name = module.resource_group.name
  tags                = module.resource_group.tags
  component           = "storage"
}

module "cost_export" {
  source = "../.."

  resource_group_id     = module.resource_group.id
  storage_account_id    = module.storage.id
  recurrence_start_date = "2026-11-01T00:00:00Z"
  reader_principal_id   = "11111111-1111-1111-1111-111111111111"
  tags                  = module.resource_group.tags
  component             = "finops"
}

output "container_id" {
  description = "Export container ID."
  value       = module.cost_export.container_id
}
