# Identities, plus the RBAC engine in rbac.tf (T-403): role assignments and federated credentials are passed in as data
# so that RBAC is reviewed in one place (envs/dev/rbac.tf), scoped per SPEC-06 section 2.
resource "azurerm_user_assigned_identity" "this" {
  for_each = var.identity_names

  name                = each.key
  resource_group_name = var.resource_group_name
  location            = var.location

  tags = merge(var.tags, { component = var.component })
}
