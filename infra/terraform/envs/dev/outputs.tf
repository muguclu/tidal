locals {
  # Identity names look like "id-api"; config keys use the short upper-case form (API, LEGACY_VM).
  identity_client_ids = {
    for name, i in module.identity.identities :
    "IDENTITY_CLIENT_ID_${upper(replace(trimprefix(name, "id-"), "-", "_"))}" => i.client_id
  }
}

output "tidal_config" {
  description = "Config-contract keys (docs/contracts/config-contract.md) and platform endpoints for a local .env.cloud. Contains no secrets; the App Insights connection string is in tidal_config_sensitive."
  value = merge(
    {
      TIDAL_ENV                           = local.env
      TIDAL_STORAGE__ACCOUNT_URL          = trimsuffix(module.storage.primary_blob_endpoint, "/")
      TIDAL_STORAGE__USE_MANAGED_IDENTITY = "true"
      TIDAL_SQL__SERVER                   = module.sql.server_fqdn
      TIDAL_SQL__DATABASE                 = module.sql.database_name
      TIDAL_SQL__AUTH                     = "entra_mi"
      TIDAL_SERVICEBUS__FQDN              = module.servicebus.fqdn
      TIDAL_AUTH__TENANT_ID               = data.azurerm_client_config.current.tenant_id
      ACR_LOGIN_SERVER                    = module.acr.login_server
      KEY_VAULT_URI                       = module.key_vault.vault_uri
    },
    local.identity_client_ids
  )
}

output "tidal_config_sensitive" {
  description = "Config-contract keys whose values are sensitive. In Azure the OTEL connection string is delivered as a Key Vault reference; locally it goes into .env.cloud."
  sensitive   = true
  value = {
    TIDAL_OTEL__CONNECTION_STRING = module.log_analytics.app_insights_connection_string
  }
}

output "resource_group_name" {
  description = "Resource group of the dev environment."
  value       = module.resource_group.name
}

output "identities" {
  description = "Managed identity name to { id, principal_id, client_id }; used by later role-assignment and compute tasks."
  value       = module.identity.identities
}

output "storage_account_name" {
  description = "Storage account name."
  value       = module.storage.name
}

output "servicebus_queue_ids" {
  description = "Service Bus queue name to resource ID."
  value       = module.servicebus.queue_ids
}

output "action_group_id" {
  description = "Budget action group ID (reused by T-506 alerts)."
  value       = module.budget.action_group_id
}
