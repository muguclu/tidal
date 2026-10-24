# RBAC engine (T-403). The environment composes every SPEC-06 section 2 row as data and passes it here, so all
# assignments created by Terraform are declared, validated and reviewed in one place (envs/dev/rbac.tf).
# This module instance may also create identities that exist only for RBAC (for example the KEDA operator).

resource "azurerm_role_assignment" "this" {
  for_each = var.role_assignments

  scope                = each.value.scope
  role_definition_name = each.value.role_definition_name
  principal_id         = each.value.principal_id
  principal_type       = each.value.principal_type
  description          = each.value.justification
}

# Federated credentials for identities created by THIS instance (AKS workload identity, SPEC-06 section 1).
resource "azurerm_federated_identity_credential" "this" {
  for_each = var.federated_credentials

  name      = each.key
  parent_id = azurerm_user_assigned_identity.this[each.value.identity_name].id
  issuer    = each.value.issuer
  subject   = each.value.subject
  audience  = ["api://AzureADTokenExchange"]
}
