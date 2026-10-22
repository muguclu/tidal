terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    # The sql module creates the free-offer database through azapi (ADR-0011).
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }
}
