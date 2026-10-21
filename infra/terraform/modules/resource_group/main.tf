locals {
  mandatory_tags = {
    project     = "tidal"
    env         = var.env
    owner       = var.owner
    cost_center = var.cost_center
    component   = var.component
    park        = tostring(var.park)
  }

  # extra_tags is validated not to collide with the mandatory keys.
  tags = merge(var.extra_tags, local.mandatory_tags)
}

resource "azurerm_resource_group" "this" {
  name     = "rg-tidal-${var.env}"
  location = var.location
  tags     = local.tags
}
