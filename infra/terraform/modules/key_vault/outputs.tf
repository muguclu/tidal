output "id" {
  description = "Key Vault resource ID. Scope secret-level role assignments (T-403) under it."
  value       = azurerm_key_vault.this.id
}

output "name" {
  description = "Key Vault name."
  value       = azurerm_key_vault.this.name
}

output "vault_uri" {
  description = "Key Vault URI (https://<name>.vault.azure.net/)."
  value       = azurerm_key_vault.this.vault_uri
}
