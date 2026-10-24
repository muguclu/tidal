output "identities" {
  description = "Map of identity name to { id, principal_id, client_id }."
  value = {
    for name, i in azurerm_user_assigned_identity.this : name => {
      id           = i.id
      principal_id = i.principal_id
      client_id    = i.client_id
    }
  }
}

output "role_assignment_ids" {
  description = "Map of logical key to role assignment resource ID for every assignment created from var.role_assignments."
  value       = { for k, r in azurerm_role_assignment.this : k => r.id }
}

output "federated_credential_ids" {
  description = "Map of credential name to federated identity credential ID."
  value       = { for k, c in azurerm_federated_identity_credential.this : k => c.id }
}
