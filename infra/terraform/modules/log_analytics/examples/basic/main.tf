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
  owner = "guclu.muge@gmail.com"
}

module "log_analytics" {
  source = "../.."

  env                 = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = merge(module.resource_group.tags, { component = "observability" })
}

output "workspace_id" {
  description = "Log Analytics workspace ID."
  value       = module.log_analytics.workspace_id
}

output "app_insights_connection_string" {
  description = "Application Insights connection string."
  value       = module.log_analytics.app_insights_connection_string
  sensitive   = true
}
