provider "azurerm" {
  subscription_id = var.subscription_id
  features {}
  # Shared keys are disabled on the state account; use Entra ID for any data-plane call.
  storage_use_azuread = true
}

provider "azuread" {}

data "azuread_client_config" "current" {}

locals {
  tags = {
    project     = "tidal"
    env         = var.env
    owner       = var.owner
    cost_center = var.cost_center
    component   = "bootstrap"
    park        = tostring(var.park)
  }

  github_issuer    = "https://token.actions.githubusercontent.com"
  github_audiences = ["api://AzureADTokenExchange"]
  github_repo_path = "${var.github_owner}/${var.github_repo}"
}

# ---------------------------------------------------------------------------
# Terraform state storage (separate account per docs/contracts/storage-layout.md section 1)
# ---------------------------------------------------------------------------
resource "azurerm_resource_group" "tfstate" {
  name     = "rg-tidal-tfstate"
  location = var.location
  tags     = merge(local.tags, { component = "tfstate" })
}

resource "azurerm_storage_account" "tfstate" {
  #checkov:skip=CKV_AZURE_206:LRS chosen deliberately (cost); state is versioned and soft-deleted and re-creatable from config
  #checkov:skip=CKV2_AZURE_1:Customer-managed keys not needed for dev state; Microsoft-managed keys plus infrastructure encryption used
  #checkov:skip=CKV_AZURE_59:GitHub-hosted runner IPs cannot be allow-listed (ranges are huge and change), so public network access stays on; access is Entra-only (shared key disabled, no SAS). Private endpoint is tracked behind the Phase 4 flag (see CKV2_AZURE_33)
  #checkov:skip=CKV2_AZURE_33:No private endpoint in dev; tracked behind the Phase 4 enable_private_networking flag
  #checkov:skip=CKV_AZURE_33:Queue service is not used; no queue logging needed
  name                = var.state_account_name
  resource_group_name = azurerm_resource_group.tfstate.name
  location            = azurerm_resource_group.tfstate.location

  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = "LRS"
  access_tier              = "Hot"

  shared_access_key_enabled         = false
  default_to_oauth_authentication   = true
  min_tls_version                   = "TLS1_2"
  https_traffic_only_enabled        = true
  allow_nested_items_to_be_public   = false
  cross_tenant_replication_enabled  = false
  infrastructure_encryption_enabled = true

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = var.state_soft_delete_days
    }

    container_delete_retention_policy {
      days = var.state_soft_delete_days
    }
  }

  tags = merge(local.tags, { component = "tfstate" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_management_lock" "tfstate_account" {
  name       = "lock-tfstate-cannotdelete"
  scope      = azurerm_storage_account.tfstate.id
  lock_level = "CanNotDelete"
  notes      = "Protects Terraform state. Remove this lock first (docs/runbooks/bootstrap.md, Rollback)."
}

# Lifecycle: noncurrent versions are kept 30 days, then deleted (versioning would otherwise grow without bound).
resource "azurerm_storage_management_policy" "tfstate" {
  storage_account_id = azurerm_storage_account.tfstate.id

  rule {
    name    = "delete-noncurrent-versions-30d"
    enabled = true

    filters {
      blob_types = ["blockBlob"]
    }

    actions {
      version {
        delete_after_days_since_creation = 30
      }
    }
  }
}

resource "azurerm_storage_container" "tfstate" {
  #checkov:skip=CKV2_AZURE_21:Blob read logging needs Log Analytics, which does not exist at bootstrap; revisit with the log_analytics module
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"

  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# CI identity: Entra app + service principal sp-tidal-ci, GitHub OIDC (no client secret is ever created)
# ---------------------------------------------------------------------------
resource "azuread_application" "ci" {
  display_name     = "sp-tidal-ci"
  owners           = [data.azuread_client_config.current.object_id]
  sign_in_audience = "AzureADMyOrg"

  notes = "Tidal CI identity (T-008). GitHub OIDC only; no secrets. Managed by infra/terraform/bootstrap."
}

resource "azuread_service_principal" "ci" {
  client_id                    = azuread_application.ci.client_id
  app_role_assignment_required = false
  owners                       = [data.azuread_client_config.current.object_id]
  tags                         = ["tidal", var.env]
}

# Jobs that declare `environment: <env>` (deploy, smoke test).
resource "azuread_application_federated_identity_credential" "github_environment" {
  application_id = azuread_application.ci.id
  display_name   = "github-env-${var.env}"
  description    = "GitHub Actions jobs running in the '${var.env}' environment"
  issuer         = local.github_issuer
  audiences      = local.github_audiences
  subject        = "repo:${local.github_repo_path}:environment:${var.env}"
}

# ---------------------------------------------------------------------------
# RBAC: none here, on purpose. sp-tidal-ci holds no role on the state account (CI never touches state; plans use the
# read-only sp-tidal-ci-plan). Its only role, AcrPush on the registry, is assigned by envs/dev/rbac.tf (T-403, SPEC-06 section 2).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Plan identity: Entra app + service principal sp-tidal-ci-plan (T-408, ADR-0008 R-18). READ-ONLY by construction.
# Used by the iac.yml jobs `plan` (pull_request, and push to main) and `rbac-drift` (schedule, workflow_dispatch). It has
# no GitHub environment, so the OIDC subject depends on the trigger. It holds NO write role, so a PR token cannot change
# anything; that is why it may be trusted for pull_request while sp-tidal-ci may not.
# ---------------------------------------------------------------------------
resource "azuread_application" "ci_plan" {
  display_name     = "sp-tidal-ci-plan"
  owners           = [data.azuread_client_config.current.object_id]
  sign_in_audience = "AzureADMyOrg"

  notes = "Tidal CI plan and drift identity (T-408). Read-only: Reader on the dev RG, Blob Reader on the tfstate container, Graph Application.Read.All. GitHub OIDC only; no secrets. Managed by infra/terraform/bootstrap."
}

resource "azuread_service_principal" "ci_plan" {
  client_id                    = azuread_application.ci_plan.client_id
  app_role_assignment_required = false
  owners                       = [data.azuread_client_config.current.object_id]
  tags                         = ["tidal", var.env]
}

# PR plans (iac.yml `plan` on pull_request, same-repo PRs only).
resource "azuread_application_federated_identity_credential" "ci_plan_pull_request" {
  application_id = azuread_application.ci_plan.id
  display_name   = "github-pull-request"
  description    = "GitHub Actions pull_request runs of this repository (read-only plan)"
  issuer         = local.github_issuer
  audiences      = local.github_audiences
  subject        = "repo:${local.github_repo_path}:pull_request"
}

# `plan` on push to main and `rbac-drift` on schedule (the cron always runs on the default branch) and on
# workflow_dispatch from main. No environment is declared on those jobs, so the subject is the branch ref.
resource "azuread_application_federated_identity_credential" "ci_plan_main" {
  application_id = azuread_application.ci_plan.id
  display_name   = "github-ref-main"
  description    = "GitHub Actions runs on refs/heads/main (push plan, nightly rbac-drift)"
  issuer         = local.github_issuer
  audiences      = local.github_audiences
  subject        = "repo:${local.github_repo_path}:ref:refs/heads/main"
}

# Reader on the dev resource group (lets `terraform plan` refresh and rbac-drift list role assignments in it).
# The group is created by envs/dev, so on a first-ever bootstrap set assign_plan_dev_rg_reader = false and apply again
# after envs/dev exists (docs/runbooks/bootstrap.md).
data "azurerm_resource_group" "dev" {
  count = var.assign_plan_dev_rg_reader ? 1 : 0
  name  = var.dev_resource_group_name
}

resource "azurerm_role_assignment" "ci_plan_dev_rg_reader" {
  count                = var.assign_plan_dev_rg_reader ? 1 : 0
  scope                = data.azurerm_resource_group.dev[0].id
  role_definition_name = "Reader"
  principal_id         = azuread_service_principal.ci_plan.object_id
  description          = "T-408: read-only plan and drift export on the dev resource group"
}

# Reads state for `terraform plan` (backend with use_azuread_auth). Scoped to the container, not the account.
resource "azurerm_role_assignment" "ci_plan_state_blob_reader" {
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azuread_service_principal.ci_plan.object_id
  description          = "T-408: read Terraform state for PR plans"
}

# Microsoft Graph Application.Read.All (application permission, read-only) for the rbac-drift job. Granting it needs
# admin consent rights in the applier (Privileged Role Administrator or Global Administrator). If the applier lacks
# them set grant_plan_graph_permission = false and grant consent by hand (docs/runbooks/bootstrap.md); drift then stays
# invisible for the Graph part until consent is given.
data "azuread_service_principal" "msgraph" {
  count     = var.grant_plan_graph_permission ? 1 : 0
  client_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph
}

resource "azuread_app_role_assignment" "ci_plan_graph_application_read_all" {
  count               = var.grant_plan_graph_permission ? 1 : 0
  app_role_id         = data.azuread_service_principal.msgraph[0].app_role_ids["Application.Read.All"]
  principal_object_id = azuread_service_principal.ci_plan.object_id
  resource_object_id  = data.azuread_service_principal.msgraph[0].object_id
}
