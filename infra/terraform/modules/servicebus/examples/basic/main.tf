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

variable "enable_ingest_topic" {
  description = "Switch the namespace to Standard and create the ingest-events topic."
  type        = bool
  default     = false
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

module "servicebus" {
  source = "../.."

  env                 = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "messaging"
  name_suffix         = "example"
  enable_ingest_topic = var.enable_ingest_topic
}

output "fqdn" {
  description = "Namespace FQDN (TIDAL_SERVICEBUS__FQDN)."
  value       = module.servicebus.fqdn
}

output "queue_ids" {
  description = "Queue name to ID map."
  value       = module.servicebus.queue_ids
}
