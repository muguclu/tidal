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

module "container_apps" {
  source = "../.."

  env                 = "dev"
  tidal_env           = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "compute"

  registry_server = "acrtidaldevexample.azurecr.io"
  image           = "acrtidaldevexample.azurecr.io/tidal:v0.1.0"

  common_env = {
    TIDAL_STORAGE__ACCOUNT_URL          = "https://sttidaldevexample.blob.core.windows.net"
    TIDAL_STORAGE__USE_MANAGED_IDENTITY = "true"
  }

  query = {
    identity_id        = module.identity.identities["id-query"].id
    identity_client_id = module.identity.identities["id-query"].client_id
  }

  jobs = {
    synth-daily = {
      trigger_type       = "schedule"
      cron_expression    = "30 6 * * *"
      identity_id        = module.identity.identities["id-ingest"].id
      identity_client_id = module.identity.identities["id-ingest"].client_id
      service_name       = "ingest"
      command            = ["tidal", "synth", "daily-history"]
    }
    tiering = {
      trigger_type       = "event"
      identity_id        = module.identity.identities["id-tiering"].id
      identity_client_id = module.identity.identities["id-tiering"].client_id
      service_name       = "tiering"
      command            = ["tidal-worker", "--queue", "tiering-jobs", "--max-messages", "1"]
      queue = {
        namespace  = "sb-tidal-dev-example"
        queue_name = "tiering-jobs"
      }
    }
  }
}

output "query_fqdn" {
  description = "Ingress FQDN of tidal-query."
  value       = module.container_apps.query_fqdn
}
