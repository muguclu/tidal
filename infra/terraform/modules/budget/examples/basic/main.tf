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

module "budget" {
  source = "../.."

  env                 = "dev"
  resource_group_name = module.resource_group.name
  resource_group_id   = module.resource_group.id
  tags                = module.resource_group.tags
  component           = "finops"
  alert_emails        = ["finops@example.invalid"]
  start_date          = "2026-11-01T00:00:00Z"
}

output "action_group_id" {
  description = "Action group ID (reused by T-506 alerts)."
  value       = module.budget.action_group_id
}

output "budget_id" {
  description = "Budget ID."
  value       = module.budget.budget_id
}
