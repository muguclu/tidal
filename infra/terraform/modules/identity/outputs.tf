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
