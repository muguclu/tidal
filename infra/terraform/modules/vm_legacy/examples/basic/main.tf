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

module "vm_legacy" {
  source = "../.."

  env                 = "dev"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "legacy-vm"

  identity_id           = module.identity.identities["id-legacy-vm"].id
  identity_principal_id = module.identity.identities["id-legacy-vm"].principal_id

  # Example key only; the VM has no inbound path at all.
  admin_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample example"
  custom_data          = "#cloud-config\nruncmd:\n  - echo example\n"
}

output "vm_name" {
  description = "Name of the legacy VM."
  value       = module.vm_legacy.vm_name
}
