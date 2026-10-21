# Identities only. All role assignments (blob, SQL, Service Bus, Key Vault, ACR pull) live in T-403
# so that RBAC is reviewed in one place, scoped per SPEC-06 section 2.
resource "azurerm_user_assigned_identity" "this" {
  for_each = var.identity_names

  name                = each.key
  resource_group_name = var.resource_group_name
  location            = var.location

  tags = merge(var.tags, { component = each.key })
}
