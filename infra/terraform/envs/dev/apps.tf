# App Service and Container Apps composition (T-309): tidal-api on App Service B1, tidal-query on Container Apps
# (min 0), and three Container Apps Jobs. Everything app-specific lives in this file so the shared main.tf,
# variables.tf and outputs.tf stay untouched (same pattern as aks.tf).

variable "enable_app_service" {
  description = "Create the App Service plan (B1) and the tidal-api web app. false removes the always-on B1 plan, the largest idle cost after ACR (docs/finops/cost-model.md, park lever)."
  type        = bool
  default     = true
}

variable "apps_image_tag" {
  description = "Tag of the tidal image used for the first revision of every app and job. The deploy workflow (T-311) rolls later tags; Terraform ignores image changes afterwards."
  type        = string
  default     = "latest"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.apps_image_tag))
    error_message = "apps_image_tag must be a valid image tag."
  }
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

  # No log shipping: the workspace cap is 0.2 GB/day (cost-model). The apps send OpenTelemetry to App Insights.
  log_analytics_workspace_id = null

  image           = local.apps_image
  registry_server = module.acr.login_server
  common_env      = local.apps_common_env

  query = {
    identity_id        = module.identity.identities["id-query"].id
    identity_client_id = module.identity.identities["id-query"].client_id
    extra_env = {
      TIDAL_AUTH__TENANT_ID = data.azurerm_client_config.current.tenant_id
    }
  }

  jobs = {
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

    # SQL hot -> Parquet cold migration, started by a message on tiering-jobs (KEDA, job identity, no secret).
    # NOTE: this job runs `tidal-tiering run`; until T-303 adds the queue consumer nothing completes the message, so
    # a message keeps re-triggering the job (one execution at a time, 30 minute cap) until its TTL or max delivery.
    tiering = {
      trigger_type       = "event"
      identity_id        = module.identity.identities["id-tiering"].id
      identity_client_id = module.identity.identities["id-tiering"].client_id
      service_name       = "tiering"
      command            = ["tidal-tiering", "run"]
      cpu                = 1
      memory             = "2Gi"
      timeout_seconds    = 1800
      max_executions     = 1
      queue = {
        namespace  = module.servicebus.name
        queue_name = "tiering-jobs"
      }
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
  }

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

  app_settings = merge(local.apps_common_env, {
    TIDAL_QUERY__BASE_URL = "https://${module.container_apps.query_fqdn}"
    TIDAL_AUTH__TENANT_ID = data.azurerm_client_config.current.tenant_id
  })

  depends_on = [azurerm_role_assignment.apps_acr_pull]
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
  description = "Container Apps Job names by short name (synth-daily, tiering, tiering-yearly); start manually with az containerapp job start."
  value       = module.container_apps.job_names
}
