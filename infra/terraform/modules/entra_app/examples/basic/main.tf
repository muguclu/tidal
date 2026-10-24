terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
  }
}

provider "azuread" {}

module "entra_app" {
  source = "../.."

  tags = {
    project     = "tidal"
    env         = "dev"
    owner       = "platform-team"
    cost_center = "tidal-dev"
    component   = "identity"
    park        = "false"
  }
  component = "identity"

  role_group_object_ids = {
    "Tidal.Reader" = ["22222222-2222-2222-2222-222222222222"] # group object ID
  }
  query_read_principal_object_ids = {
    "id-api" = "11111111-1111-1111-1111-111111111111" # principal_id of the id-api managed identity
  }
}

output "api" {
  description = "tidal-api registration details."
  value       = module.entra_app.api
}
