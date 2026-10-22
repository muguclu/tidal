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
  source = "../.."

  name                = "sttidaldevexample01"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "storage"
}

output "container_ids" {
  description = "Container name to ID map (RBAC scoping in T-403)."
  value       = module.storage.container_ids
}

output "primary_blob_endpoint" {
  description = "Primary blob endpoint."
  value       = module.storage.primary_blob_endpoint
}
