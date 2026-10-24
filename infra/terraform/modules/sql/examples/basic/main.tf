terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }
}

variable "subscription_id" {
  description = "Azure subscription GUID (dummy value is fine for validate)."
  type        = string
  default     = "00000000-0000-0000-0000-000000000000"
}

provider "azapi" {}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

module "resource_group" {
  source = "../../../resource_group"

  env   = "dev"
  owner = "platform-team"
}

module "sql" {
  source = "../.."

  env                   = "dev"
  resource_group_name   = module.resource_group.name
  location              = module.resource_group.location
  tags                  = module.resource_group.tags
  component             = "data"
  tenant_id             = "00000000-0000-0000-0000-000000000000"
  entra_admin_login     = "sg-tidal-sql-admins"
  entra_admin_object_id = "11111111-1111-1111-1111-111111111111"
  server_name_suffix    = "example"

  # Dummy ID for validate; the env composition passes module.log_analytics.workspace_id.
  log_analytics_workspace_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.OperationalInsights/workspaces/log-example"
}

output "server_fqdn" {
  description = "SQL server FQDN (TIDAL_SQL__SERVER)."
  value       = module.sql.server_fqdn
}

output "database_name" {
  description = "Database name (TIDAL_SQL__DATABASE)."
  value       = module.sql.database_name
}
