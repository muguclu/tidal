terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    # Entra app registrations and role assignments (entra.tf, T-401).
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    # The sql module creates the free-offer database through azapi (ADR-0011).
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    # Generates the cursor HMAC key in rbac.tf (T-403); the value lands in Terraform state.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
