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

module "identity" {
  source = "../../../identity"

  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "identity"
}

module "app_service" {
  source = "../.."

  env                 = "dev"
  tidal_env           = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  name_suffix         = "example"
  tags                = module.resource_group.tags
  component           = "compute"

  registry_server    = "acrtidaldevexample.azurecr.io"
  identity_id        = module.identity.identities["id-api"].id
  identity_client_id = module.identity.identities["id-api"].client_id

  app_settings = {
    TIDAL_STORAGE__ACCOUNT_URL = "https://sttidaldevexample.blob.core.windows.net"
  }
}

output "hostname" {
  description = "Default host name of the web app."
  value       = module.app_service.default_hostname
}
