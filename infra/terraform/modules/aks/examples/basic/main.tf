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

module "aks" {
  source = "../.."

  env                 = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tenant_id           = "00000000-0000-0000-0000-000000000000"
  tags                = module.resource_group.tags
  component           = "compute"

  cluster_admin_principal_ids = ["11111111-1111-1111-1111-111111111111"]

  workload_identities = {
    worker = {
      identity_id     = module.identity.identities["id-worker"].id
      namespace       = "tidal"
      service_account = "tidal-worker"
    }
  }
}

output "cluster_name" {
  description = "AKS cluster name."
  value       = module.aks.name
}

output "kubelet_identity_object_id" {
  description = "Kubelet identity that needs AcrPull."
  value       = module.aks.kubelet_identity_object_id
}
