# App Service and Container Apps composition (T-309): tidal-api on App Service B1, tidal-query on Container Apps
# (min 0), and three Container Apps Jobs. Everything app-specific lives in this file so the shared main.tf,
# variables.tf and outputs.tf stay untouched (same pattern as aks.tf).

variable "enable_app_service" {
  description = "Create the App Service plan (B1) and the tidal-api web app. false removes the always-on B1 plan, the largest idle cost after ACR (docs/finops/cost-model.md, park lever)."
  type        = bool
  default     = true
}

variable "apps_image_tag" {
  description = "Tag of the tidal image used for the first revision of every app and job (for example v0.1.0 or sha-abc1234). Required and never latest. The deploy workflow (T-311) rolls later tags; Terraform ignores image changes afterwards."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.apps_image_tag)) && var.apps_image_tag != "latest"
    error_message = "apps_image_tag must be a valid image tag and must not be latest."
  }
}

variable "enable_tiering_event_job" {
  description = "Create the Service Bus triggered tiering job. true since T-403: envs/dev/rbac.tf grants id-tiering (the job identity, which also authenticates the KEDA scale rule) Data Owner on the tiering-jobs queue only, because queue counts need Manage rights. Set false to remove the job (the role then drops back to Data Receiver)."
  type        = bool
  default     = true
}

locals {
  apps_image = "${module.acr.login_server}/tidal:${var.apps_image_tag}"

  # Non-secret config-contract keys shared by every app and job (docs/contracts/config-contract.md). TIDAL_ENV,
  # TIDAL_SERVICE_NAME and AZURE_CLIENT_ID are added by the modules.
  apps_common_env = {
    TIDAL_STORAGE__ACCOUNT_URL          = trimsuffix(module.storage.primary_blob_endpoint, "/")
    TIDAL_STORAGE__USE_MANAGED_IDENTITY = "true"
    TIDAL_SQL__SERVER                   = module.sql.server_fqdn
    TIDAL_SQL__DATABASE                 = module.sql.database_name
    TIDAL_SQL__AUTH                     = "entra_mi"
    TIDAL_SERVICEBUS__FQDN              = module.servicebus.fqdn
  }

  # Key Vault URL (trailing slash removed) and the versionless URI of the App Insights connection string secret
  # (rbac.tf). The value is never in state or in the settings: App Service resolves the reference with the api identity,
  # Container Apps resolves the secret with the query identity (both hold Key Vault Secrets User on that one secret).
  apps_key_vault_url        = trimsuffix(module.key_vault.vault_uri, "/")
  apps_otel_secret_uri      = "${local.apps_key_vault_url}/secrets/${azapi_resource.appinsights_connection_string.name}"
  apps_otel_app_service_ref = "@Microsoft.KeyVault(SecretUri=${local.apps_otel_secret_uri})"

  # Entra-derived keys per app (local.entra_config in entra.tf). Without them the api rejects every azp
  # and tidal-query refuses to start (empty caller allowlists).
  apps_api_entra_env = {
    for k in ["TIDAL_AUTH__AUDIENCE", "TIDAL_AUTH__API_APP_CLIENT_ID", "TIDAL_AUTH__ALLOWED_CLIENT_IDS", "TIDAL_QUERY__AUDIENCE", "TIDAL_QUERY__APP_CLIENT_ID"] :
    k => local.entra_config[k]
  }
  apps_query_entra_env = {
    for k in ["TIDAL_QUERY__AUDIENCE", "TIDAL_QUERY__APP_CLIENT_ID", "TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS", "TIDAL_AUTH__QUERY_CALLER_OBJECT_IDS"] :
    k => local.entra_config[k]
  }

  # Identities that pull the tidal image from ACR (SPEC-06 section 2 lists no ACR role for them; AcrPull is the
  # least-privilege way to pull with a managed identity, scoped to the registry only).
  apps_acr_pull_identities = merge(
    { "id-query" = module.identity.identities["id-query"].principal_id },
    { "id-ingest" = module.identity.identities["id-ingest"].principal_id },
    { "id-tiering" = module.identity.identities["id-tiering"].principal_id },
    var.enable_app_service ? { "id-api" = module.identity.identities["id-api"].principal_id } : {}
  )

  # SPEC-03 section 3.1: yearly, retag first, then compact every dataset. retag failing stops the job (compaction
  # relies on fresh age_band tags); a failing dataset does not stop the others, but fails the run.
  yearly_retag_compact_script = <<-SH
    set -u
    tidal-tiering retag || exit 1
    rc=0
    for d in ticks bars_1s bars_1m bars_1h bars_1d; do
      tidal-tiering compact --dataset "$d" || rc=1
    done
    exit "$rc"
  SH
}

resource "azurerm_role_assignment" "apps_acr_pull" {
  for_each = local.apps_acr_pull_identities

  scope                = module.acr.id
  role_definition_name = "AcrPull"
  principal_id         = each.value
  principal_type       = "ServicePrincipal"
}

module "container_apps" {
  source = "../../modules/container_apps"

  env                 = local.env
  tidal_env           = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "compute"

  # Environment logs go to the shared workspace, which already has the 0.2 GB/day cap (cost-model).
  # TIDAL_OTEL__CONNECTION_STRING reaches tidal-query as a Container Apps secret backed by Key Vault (query.key_vault_env).
  log_analytics_workspace_id = module.log_analytics.workspace_id

  # Flag on (network.tf): the environment moves into snet-aca with an internal load balancer, so tidal-query is reachable
  # from the VNet only (ingress stays https, allow_insecure_connections = false). Flag off: public environment.
  infrastructure_subnet_id       = module.network.aca_subnet_id
  internal_load_balancer_enabled = var.enable_private_networking

  image           = local.apps_image
  registry_server = module.acr.login_server
  common_env      = local.apps_common_env

  query = {
    identity_id        = module.identity.identities["id-query"].id
    identity_client_id = module.identity.identities["id-query"].client_id
    extra_env = merge(local.apps_query_entra_env, {
      TIDAL_AUTH__TENANT_ID = data.azurerm_client_config.current.tenant_id
    })
    key_vault_env = {
      TIDAL_OTEL__CONNECTION_STRING = local.apps_otel_secret_uri
    }
  }

  jobs = merge({
    # Daily synthetic bars (ADR-0013; there is no vendor ingest). Idempotent: unchanged cold bars are skipped.
    synth-daily = {
      trigger_type       = "schedule"
      cron_expression    = "30 6 * * *"
      identity_id        = module.identity.identities["id-ingest"].id
      identity_client_id = module.identity.identities["id-ingest"].client_id
      service_name       = "ingest"
      command            = ["tidal", "synth", "daily-history"]
      cpu                = 1
      memory             = "2Gi"
      timeout_seconds    = 3600
    }

    # Jan 2, 03:00 UTC: retag then compact all five datasets (SPEC-03 section 3.1, ADR-0015).
    tiering-yearly = {
      trigger_type       = "schedule"
      cron_expression    = "0 3 2 1 *"
      identity_id        = module.identity.identities["id-tiering"].id
      identity_client_id = module.identity.identities["id-tiering"].client_id
      service_name       = "tiering"
      command            = ["/bin/sh", "-c"]
      args               = [local.yearly_retag_compact_script]
      cpu                = 2
      memory             = "4Gi"
      timeout_seconds    = 21600
      retry_limit        = 0
    }
    },
    # SQL hot -> Parquet cold migration, started by a message on tiering-jobs (KEDA scaler with the job identity, no
    # secret). On by default since T-403: the scaler reads queue counts, which needs Data Owner on that queue (rbac.tf).
    var.enable_tiering_event_job ? {
      tiering = {
        trigger_type       = "event"
        identity_id        = module.identity.identities["id-tiering"].id
        identity_client_id = module.identity.identities["id-tiering"].client_id
        service_name       = "tiering"
        command            = ["tidal-worker", "--queue", "tiering-jobs", "--max-messages", "1"]
        cpu                = 1
        memory             = "2Gi"
        timeout_seconds    = 1800
        max_executions     = 1
        queue = {
          namespace  = module.servicebus.name
          queue_name = "tiering-jobs"
        }
      }
    } : {}
  )

  # The identities must be able to pull before the first revision is created.
  depends_on = [azurerm_role_assignment.apps_acr_pull]
}

module "app_service" {
  source = "../../modules/app_service"
  count  = var.enable_app_service ? 1 : 0

  env                 = local.env
  tidal_env           = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  name_suffix         = var.name_suffix
  tags                = module.resource_group.tags
  component           = "compute"

  registry_server    = module.acr.login_server
  image_name         = "tidal:${var.apps_image_tag}"
  identity_id        = module.identity.identities["id-api"].id
  identity_client_id = module.identity.identities["id-api"].client_id

  # snet-app when enable_private_networking is true (network.tf), null otherwise.
  vnet_integration_subnet_id = module.network.app_subnet_id

  app_settings = merge(local.apps_common_env, local.apps_api_entra_env, {
    TIDAL_QUERY__BASE_URL         = "https://${module.container_apps.query_fqdn}"
    TIDAL_AUTH__TENANT_ID         = data.azurerm_client_config.current.tenant_id
    TIDAL_CURSOR__KEYVAULT_URL    = local.apps_key_vault_url
    TIDAL_OTEL__CONNECTION_STRING = local.apps_otel_app_service_ref
  })

  depends_on = [azurerm_role_assignment.apps_acr_pull]
}

# Flag off: only the web app's outbound IPs may call tidal-query. Patched with azapi because the web app needs the query
# FQDN (a module-level input would be a cycle). IP filtering is not identity: T-402 must also check caller oid/azp == id-api.
# Flag on: the environment is internal and the web app calls it from snet-app (a private source address, not one of the
# public outbound IPs), so the allow list is replaced by an empty list; the VNet is the boundary and the JWT check stays.
resource "azapi_update_resource" "query_ip_allow_list" {
  count = var.enable_app_service ? 1 : 0

  type        = "Microsoft.App/containerApps@2024-03-01"
  resource_id = module.container_apps.query_app_id

  body = {
    properties = {
      configuration = {
        ingress = {
          ipSecurityRestrictions = var.enable_private_networking ? [] : [
            for ip in module.app_service[0].possible_outbound_ip_addresses : {
              name           = "allow-${replace(ip, ".", "-")}"
              action         = "Allow"
              ipAddressRange = "${ip}/32"
            }
          ]
        }
      }
    }
  }
}

output "api_hostname" {
  description = "Default host name of the tidal-api web app, or null when enable_app_service is false."
  value       = one(module.app_service[*].default_hostname)
}

output "query_fqdn" {
  description = "Ingress FQDN of the tidal-query Container App (scales to zero; the first request after idle cold-starts)."
  value       = module.container_apps.query_fqdn
}

output "apps_job_names" {
  description = "Container Apps Job names by short name (synth-daily, tiering-yearly, and tiering when enable_tiering_event_job is true); start manually with az containerapp job start."
  value       = module.container_apps.job_names
}
