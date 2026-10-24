# RBAC for the dev environment (T-403, SPEC-06 section 2). The single place that declares which identity may do what.
# Rows that live elsewhere and are NOT repeated here (each SPEC-06 row is implemented exactly once):
#   AcrPull for id-api (while enable_app_service), id-query, id-ingest, id-tiering   apps.tf  apps_acr_pull
#   AcrPull for the AKS kubelet                                                      aks.tf   aks_kubelet_acr_pull
#   id-legacy-vm blob roles                                                          vm.tf    legacy_vm_*
#   id-legacy-vm self-deallocate and the Logic App start role                        modules/vm_legacy
#   AKS RBAC Cluster Admin for the applier and extra admins                          modules/aks
#   Entra app roles (Tidal.Query.Read for id-api and id-worker)                      entra.tf (T-401)
# SQL roles are T-SQL, not Azure RBAC: docs/rbac/sql-grants.sql (output sql_grants_hint).
# Drift: scripts/rbac-export.sh exports the live state and CI diffs it against docs/rbac/expected.json.

variable "ci_client_id" {
  description = "Client ID of the sp-tidal-ci app created by infra/terraform/bootstrap. It receives AcrPush on the registry only. null skips the assignment."
  type        = string
  default     = null

  validation {
    condition     = var.ci_client_id == null || can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", coalesce(var.ci_client_id, "x")))
    error_message = "ci_client_id must be a GUID or null."
  }
}

variable "github_repo_path" {
  description = "GitHub owner/repo (for example muguclu/tidal). Creates the deploy-only app sp-tidal-deploy with a federated credential for the environment named in deploy_github_environment. null skips the app and its roles (deploy then keeps using whatever AZURE_CLIENT_ID points to)."
  type        = string
  default     = null

  validation {
    condition     = var.github_repo_path == null || can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", coalesce(var.github_repo_path, "x")))
    error_message = "github_repo_path must look like owner/repo or be null."
  }
}

variable "deploy_github_environment" {
  description = "GitHub environment whose jobs may use sp-tidal-deploy (federated subject repo:<owner/repo>:environment:<name>). Must differ from the environment of sp-tidal-ci (dev) so each app has exactly one subject and the deploy rights are unreachable from image-build jobs; protect it as described in docs/runbooks/deploy.md (main only, required reviewer)."
  type        = string
  default     = "dev-deploy"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+$", var.deploy_github_environment))
    error_message = "deploy_github_environment may contain letters, digits, '.', '_' and '-'."
  }

  validation {
    condition     = var.deploy_github_environment != "dev"
    error_message = "deploy_github_environment must not be dev: that subject belongs to sp-tidal-ci (identity isolation)."
  }
}

variable "k8s_namespace" {
  description = "Kubernetes namespace of the Tidal chart (T-307); sp-tidal-deploy gets AKS RBAC Writer on this namespace only."
  type        = string
  default     = "tidal"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.k8s_namespace))
    error_message = "k8s_namespace must be a valid Kubernetes namespace name."
  }
}

# --- Secrets in Key Vault ----------------------------------------------------------------------
# Both secrets are created through the Azure Resource Manager API (azapi, Microsoft.KeyVault/vaults/secrets), not the
# data plane, for two reasons:
#   - Reader-only plan: refreshing an azurerm_key_vault_secret calls GetSecret on the data plane, which the Reader-only
#     sp-tidal-ci-plan cannot do; an ARM GET never returns the value, so the PR plan job keeps working with Reader.
#   - No secret in state: `sensitive_body` is write-only (never stored in state or plan). The cursor key comes from an
#     ephemeral random_bytes, so it exists only in memory during the apply that writes it.
# The applier needs Microsoft.KeyVault/vaults/secrets/write (Contributor has it); no data-plane role is required.
# Consumers read through the data-plane roles on the individual secrets (Secrets User, below).

variable "cursor_hmac_version" {
  description = "Version of the cursor-hmac-key value. The key is generated and written only when this number changes (write-only values leave no trace in state to diff against). Increase it to rotate; running API instances must restart to pick up the new version."
  type        = number
  default     = 1

  validation {
    condition     = var.cursor_hmac_version >= 1 && floor(var.cursor_hmac_version) == var.cursor_hmac_version
    error_message = "cursor_hmac_version must be a whole number of at least 1."
  }
}

# 32 random bytes for signing pagination cursors (T-402), base64. Ephemeral: never persisted.
ephemeral "random_bytes" "cursor_hmac" {
  length = 32
}

resource "azapi_resource" "cursor_hmac" {
  type      = "Microsoft.KeyVault/vaults/secrets@2023-07-01"
  name      = "cursor-hmac-key"
  parent_id = module.key_vault.id

  body = {
    properties = {
      contentType = "text/plain; base64 HMAC key for API cursors"
      # No expiry on purpose: an expiring signing key would silently break paging. Rotation is explicit (cursor_hmac_version).
      attributes = { enabled = true }
    }
  }

  sensitive_body         = { properties = { value = ephemeral.random_bytes.cursor_hmac.base64 } }
  sensitive_body_version = { "properties.value" = tostring(var.cursor_hmac_version) }

  tags = merge(module.resource_group.tags, { component = "secrets" })
}

# SPEC-06 section 3: the Key Vault holds the App Insights connection string. The apps do not reference it yet
# (apps.tf still leaves TIDAL_OTEL__CONNECTION_STRING unwired); the secret and the reader roles are ready for that.
# The version is a hash of the value, so a changed connection string is rewritten without the value being kept in state.
resource "azapi_resource" "appinsights_connection_string" {
  type      = "Microsoft.KeyVault/vaults/secrets@2023-07-01"
  name      = "appinsights-connection-string"
  parent_id = module.key_vault.id

  body = {
    properties = {
      contentType = "text/plain; Application Insights connection string"
      attributes  = { enabled = true }
    }
  }

  sensitive_body         = { properties = { value = module.log_analytics.app_insights_connection_string } }
  sensitive_body_version = { "properties.value" = substr(sha256(module.log_analytics.app_insights_connection_string), 0, 16) }

  tags = merge(module.resource_group.tags, { component = "secrets" })
}

# --- Deploy-only OIDC app (separate from sp-tidal-ci, which only pushes images) ---------------------

resource "azuread_application" "deploy" {
  count = var.github_repo_path == null ? 0 : 1

  display_name     = "sp-tidal-deploy"
  sign_in_audience = "AzureADMyOrg"
  owners           = var.entra_owner_object_ids
  notes            = "Tidal deploy identity (T-403). GitHub OIDC only; no secrets. Roles are scoped to the deploy targets in envs/dev/rbac.tf."
}

resource "azuread_service_principal" "deploy" {
  count = var.github_repo_path == null ? 0 : 1

  client_id                    = azuread_application.deploy[0].client_id
  app_role_assignment_required = false
  owners                       = var.entra_owner_object_ids
  tags                         = ["tidal", local.env]
}

resource "azuread_application_federated_identity_credential" "deploy" {
  # checkov:skip=CKV_AZURE_249: the subject is repo:<owner/repo>:environment:<name> built from validated variables (no wildcard); checkov cannot resolve a variable whose default is null.
  count = var.github_repo_path == null ? 0 : 1

  application_id = azuread_application.deploy[0].id
  display_name   = "github-env-${var.deploy_github_environment}"
  description    = "GitHub Actions jobs running in the '${var.deploy_github_environment}' environment"
  issuer         = "https://token.actions.githubusercontent.com"
  audiences      = ["api://AzureADTokenExchange"]
  subject        = "repo:${var.github_repo_path}:environment:${var.deploy_github_environment}"
}

data "azuread_service_principal" "ci" {
  count = var.ci_client_id == null ? 0 : 1

  client_id = var.ci_client_id
}

# --- KEDA operator and the assignments ---------------------------------------------------------------
# A second instance of the identity module: it creates id-keda-operator, its federated credential (the AKS KEDA
# add-on runs as kube-system/keda-operator) and every role assignment below.

module "rbac" {
  source = "../../modules/identity"

  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "identity"

  identity_names = ["id-keda-operator"]

  federated_credentials = {
    keda-operator = {
      identity_name = "id-keda-operator"
      issuer        = module.aks.oidc_issuer_url
      subject       = "system:serviceaccount:kube-system:keda-operator"
    }
  }

  role_assignments = local.rbac_assignments
}

locals {
  principals = merge(
    { for n, i in module.identity.identities : n => i.principal_id },
    { "id-keda-operator" = module.rbac.identities["id-keda-operator"].principal_id },
    var.ci_client_id == null ? {} : { "sp-tidal-ci" = one(data.azuread_service_principal.ci[*].object_id) },
    var.github_repo_path == null ? {} : { "sp-tidal-deploy" = one(azuread_service_principal.deploy[*].object_id) },
  )

  # Short scope names used in the rows below.
  scopes = merge(
    { for n, id in module.storage.container_ids : "container:${n}" => id },
    { for n, id in module.servicebus.queue_ids : "queue:${n}" => id },
    {
      "secret:cursor-hmac-key"               = "${module.key_vault.id}/secrets/${azapi_resource.cursor_hmac.name}"
      "secret:appinsights-connection-string" = "${module.key_vault.id}/secrets/${azapi_resource.appinsights_connection_string.name}"
      "appinsights"                          = module.log_analytics.app_insights_id
      "acr"                                  = module.acr.id
      "aks"                                  = module.aks.id
      "aks-namespace"                        = "${module.aks.id}/namespaces/${var.k8s_namespace}"
      "containerapp:query"                   = module.container_apps.query_app_id
    },
    { for n, id in module.container_apps.job_ids : "job:${n}" => id },
    { for n, i in module.identity.identities : "identity:${n}" => i.id },
    var.enable_app_service ? { "webapp" = one(module.app_service[*].id) } : {},
  )

  blob_reader      = "Storage Blob Data Reader"
  blob_contributor = "Storage Blob Data Contributor"
  blob_owner       = "Storage Blob Data Owner"
  sb_sender        = "Azure Service Bus Data Sender"
  sb_receiver      = "Azure Service Bus Data Receiver"
  sb_owner         = "Azure Service Bus Data Owner"
  kv_secrets_user  = "Key Vault Secrets User"
  metrics_pub      = "Monitoring Metrics Publisher"

  # id-tiering also drives the KEDA scaler of the event job (Container Apps authenticates the scale rule with the job
  # identity, modules/container_apps). Queue length needs Manage rights, so while the job exists the Receiver role on
  # tiering-jobs becomes Data Owner on that one queue. One assignment either way.
  tiering_queue_role = var.enable_tiering_event_job ? local.sb_owner : local.sb_receiver

  # who, role, on (key of local.scopes), why. One row = one assignment. "why" becomes the assignment description.
  rbac_rows = concat(
    [
      # id-api: serves reads from Blob (never writes), enqueues work, reads results.
      { who = "id-api", role = local.blob_reader, on = "container:marketdata", why = "SPEC-06 s2: API reads market data" },
      { who = "id-api", role = local.blob_reader, on = "container:reference", why = "SPEC-06 s2: API reads reference data" },
      { who = "id-api", role = local.blob_reader, on = "container:backtests", why = "SPEC-06 s2: API reads backtest results" },
      { who = "id-api", role = local.sb_sender, on = "queue:backtest-jobs", why = "SPEC-06 s2: API submits backtests" },
      { who = "id-api", role = local.sb_sender, on = "queue:tiering-jobs", why = "SPEC-06 s2: API requests tiering runs" },
      { who = "id-api", role = local.sb_receiver, on = "queue:backtest-results", why = "SPEC-06 s2: API consumes backtest results" },
      { who = "id-api", role = local.kv_secrets_user, on = "secret:cursor-hmac-key", why = "T-402: signs and verifies pagination cursors" },
      { who = "id-api", role = local.kv_secrets_user, on = "secret:appinsights-connection-string", why = "SPEC-06 s3: telemetry connection string via Key Vault reference" },

      # id-query: read-only on market data and reference.
      { who = "id-query", role = local.blob_reader, on = "container:marketdata", why = "SPEC-06 s2: query reads cold bars" },
      { who = "id-query", role = local.blob_reader, on = "container:reference", why = "SPEC-06 s2: query reads reference data" },
      { who = "id-query", role = local.kv_secrets_user, on = "secret:appinsights-connection-string", why = "SPEC-06 s3: telemetry connection string via Key Vault reference" },

      # id-worker: no SQL (ADR-0016) and no Key Vault. Reads data through tidal-query (Entra role in entra.tf).
      { who = "id-worker", role = local.blob_reader, on = "container:marketdata", why = "SPEC-06 s2: worker reads market data" },
      { who = "id-worker", role = local.blob_reader, on = "container:reference", why = "SPEC-06 s2: worker reads reference data" },
      { who = "id-worker", role = local.blob_contributor, on = "container:backtests", why = "SPEC-06 s2: worker writes backtest outputs" },
      { who = "id-worker", role = local.sb_receiver, on = "queue:backtest-jobs", why = "SPEC-06 s2: worker consumes jobs (scaler uses id-keda-operator)" },
      { who = "id-worker", role = local.sb_sender, on = "queue:backtest-results", why = "SPEC-06 s2: worker publishes results" },

      # id-ingest: writes raw and cold data. Contributor, not Owner: no blob tags, no ACLs.
      { who = "id-ingest", role = local.blob_contributor, on = "container:raw", why = "SPEC-06 s2: ingest lands raw files" },
      { who = "id-ingest", role = local.blob_contributor, on = "container:marketdata", why = "SPEC-06 s2: ingest writes cold Parquet" },
      { who = "id-ingest", role = local.blob_contributor, on = "container:reference", why = "SPEC-06 s2: ingest writes reference data" },

      # id-tiering: blob index tags need tags/write, which Contributor lacks. Owner on marketdata only; a custom
      # tags/write role was not adopted (cannot be verified without Azure, see docs/rbac/README.md).
      { who = "id-tiering", role = local.blob_owner, on = "container:marketdata", why = "SPEC-06 s2: tiering compacts and retags cold Parquet (blob tags/write)" },
      { who = "id-tiering", role = local.tiering_queue_role, on = "queue:tiering-jobs", why = "SPEC-06 s2: tiering consumes jobs; Owner while it is also the KEDA scaler identity of the event job" },

      # T-506: Application Insights accepts Entra tokens only (local authentication is off). Each telemetry-emitting
      # identity may publish to the one App Insights resource and nothing else.
      { who = "id-api", role = local.metrics_pub, on = "appinsights", why = "T-506: API publishes telemetry to Application Insights (Entra auth only)" },
      { who = "id-query", role = local.metrics_pub, on = "appinsights", why = "T-506: query service publishes telemetry to Application Insights (Entra auth only)" },
      { who = "id-worker", role = local.metrics_pub, on = "appinsights", why = "T-506: worker publishes telemetry to Application Insights (Entra auth only)" },
      { who = "id-ingest", role = local.metrics_pub, on = "appinsights", why = "T-506: ingest publishes telemetry to Application Insights (Entra auth only)" },
      { who = "id-tiering", role = local.metrics_pub, on = "appinsights", why = "T-506: tiering publishes telemetry to Application Insights (Entra auth only)" },

      # KEDA operator: queue length reads need management rights. Only this queue.
      { who = "id-keda-operator", role = local.sb_owner, on = "queue:backtest-jobs", why = "SPEC-06 s2: KEDA scaler reads backtest-jobs queue length" },
    ],

    # sp-tidal-ci pushes images and nothing else. Contributor on the resource group is not granted (SPEC-06 s2 edit):
    # app updates are done by sp-tidal-deploy on the individual targets.
    var.ci_client_id == null ? [] : [
      { who = "sp-tidal-ci", role = "AcrPush", on = "acr", why = "SPEC-06 s2: CI pushes images to the registry only" },
    ],

    # sp-tidal-deploy: one role per deploy step in .github/workflows/deploy.yml, each on its own target.
    var.github_repo_path == null ? [] : concat(
      [
        { who = "sp-tidal-deploy", role = "AcrPull", on = "acr", why = "deploy.yml: az acr repository show / manifest show-metadata to resolve digests" },
        { who = "sp-tidal-deploy", role = "Contributor", on = "containerapp:query", why = "deploy.yml: az containerapp update on ca-tidal-query only" },
        { who = "sp-tidal-deploy", role = "Reader", on = "aks", why = "deploy.yml: az aks show reads the power state" },
        { who = "sp-tidal-deploy", role = "Azure Kubernetes Service Cluster User Role", on = "aks", why = "deploy.yml: az aks get-credentials (local accounts are disabled)" },
        { who = "sp-tidal-deploy", role = "Azure Kubernetes Service RBAC Writer", on = "aks-namespace", why = "deploy.yml: helm upgrade in the tidal namespace only" },
        # Updating a resource that carries a user-assigned identity needs assign/action on that identity.
        { who = "sp-tidal-deploy", role = "Managed Identity Operator", on = "identity:id-query", why = "deploy.yml: updating ca-tidal-query re-submits its identity" },
        { who = "sp-tidal-deploy", role = "Managed Identity Operator", on = "identity:id-ingest", why = "deploy.yml: updating the synth-daily job re-submits its identity" },
        { who = "sp-tidal-deploy", role = "Managed Identity Operator", on = "identity:id-tiering", why = "deploy.yml: updating the tiering jobs re-submits their identity" },
      ],
      [for n, _ in module.container_apps.job_ids : { who = "sp-tidal-deploy", role = "Contributor", on = "job:${n}", why = "deploy.yml: az containerapp job update on this job only" }],
      var.enable_app_service ? [
        { who = "sp-tidal-deploy", role = "Website Contributor", on = "webapp", why = "deploy.yml: az webapp config container set on the web app only" },
        { who = "sp-tidal-deploy", role = "Managed Identity Operator", on = "identity:id-api", why = "deploy.yml: updating the web app re-submits its identity" },
      ] : [],
    ),
  )

  rbac_assignments = {
    for r in local.rbac_rows : "${r.who}|${r.role}|${r.on}" => {
      principal_id         = local.principals[r.who]
      role_definition_name = r.role
      scope                = local.scopes[r.on]
      justification        = r.why
    }
  }
}

output "keda_identity" {
  description = "KEDA operator identity for the Helm chart (T-307 keda.identityClientId) and the GitHub environment variable TIDAL_KEDA_CLIENT_ID."
  value = {
    client_id    = module.rbac.identities["id-keda-operator"].client_id
    principal_id = module.rbac.identities["id-keda-operator"].principal_id
  }
}

output "deploy_client_id" {
  description = "Client ID of sp-tidal-deploy (GitHub environment variable AZURE_CLIENT_ID for deploy), or null when github_repo_path is null."
  value       = one(azuread_application.deploy[*].client_id)
}

output "sql_grants_hint" {
  description = "SQL roles are database objects, not Azure RBAC. Run docs/rbac/sql-grants.sql as the SQL Entra admin after the first apply."
  value       = "docs/rbac/sql-grants.sql"
}
