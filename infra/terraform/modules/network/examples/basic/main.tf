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

  name                      = "sttidaldevexample"
  resource_group_name       = module.resource_group.name
  location                  = module.resource_group.location
  enable_private_networking = true
  tags                      = module.resource_group.tags
  component                 = "storage"
}

module "network" {
  source = "../.."

  enabled             = true
  env                 = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags

  private_endpoint_targets = {
    blob = module.storage.id
  }
}

output "subnet_ids" {
  description = "Subnet IDs by short name."
  value       = module.network.subnet_ids
}
