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
# Plan identity: Entra app + service principal sp-tidal-ci-plan (T-408, ADR-0008 R-18). NO WRITE ROLE.
# Used by the iac.yml job `plan` (pull_request, and push to main). It has no GitHub environment, so the OIDC subject
# depends on the trigger. Any same-repo PR can mint its token, so it can read the full dev state and make tenant-wide
# Graph app reads: that exposure is accepted in ADR-0008 R-27. Graph Application.Read.All is needed because the PR plan
# refreshes the azuread resources of envs/dev. The nightly drift job does NOT use this app (see sp-tidal-rbac-drift).
# ---------------------------------------------------------------------------
resource "azuread_application" "ci_plan" {
  display_name     = "sp-tidal-ci-plan"
  owners           = [data.azuread_client_config.current.object_id]
  sign_in_audience = "AzureADMyOrg"

  notes = "Tidal CI plan identity (T-408). No write role: Reader on the dev RG, Blob Reader on the tfstate container, Graph Application.Read.All. GitHub OIDC only; no secrets. Managed by infra/terraform/bootstrap."
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

# `plan` on push to main. No environment is declared on that job, so the subject is the branch ref.
resource "azuread_application_federated_identity_credential" "ci_plan_main" {
  application_id = azuread_application.ci_plan.id
  display_name   = "github-ref-main"
  description    = "GitHub Actions runs on refs/heads/main (push plan)"
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

# Microsoft Graph Application.Read.All (application permission, read-only): the PR plan refreshes the azuread resources
# of envs/dev (the same permission also serves sp-tidal-rbac-drift below). Granting it needs
# admin consent rights in the applier (Privileged Role Administrator or Global Administrator). If the applier lacks
# them set grant_plan_graph_permission = false and grant consent by hand (docs/runbooks/bootstrap.md); until then the PR plan
# cannot refresh azuread resources and the drift job fails at its Graph calls.
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

# ---------------------------------------------------------------------------
# Drift identity: sp-tidal-rbac-drift (T-408 review). Separate from the plan app so the nightly export needs neither the
# state role nor a PR-reachable subject. Subject: GitHub environment `rbac-drift`, which MUST be restricted to the main
# branch (deployment branch policy) so only main can mint the token. No state role. Role: custom, read-only.
# ---------------------------------------------------------------------------
resource "azuread_application" "rbac_drift" {
  display_name     = "sp-tidal-rbac-drift"
  owners           = [data.azuread_client_config.current.object_id]
  sign_in_audience = "AzureADMyOrg"

  notes = "Tidal nightly RBAC drift export (T-408). Custom read-only subscription role plus Graph Application.Read.All; no state access. GitHub OIDC only, environment rbac-drift (main only). Managed by infra/terraform/bootstrap."
}

resource "azuread_service_principal" "rbac_drift" {
  client_id                    = azuread_application.rbac_drift.client_id
  app_role_assignment_required = false
  owners                       = [data.azuread_client_config.current.object_id]
  tags                         = ["tidal", var.env]
}

resource "azuread_application_federated_identity_credential" "rbac_drift_environment" {
  application_id = azuread_application.rbac_drift.id
  display_name   = "github-env-${var.rbac_drift_environment}"
  description    = "GitHub Actions jobs in the '${var.rbac_drift_environment}' environment (restricted to main)"
  issuer         = local.github_issuer
  audiences      = local.github_audiences
  subject        = "repo:${local.github_repo_path}:environment:${var.rbac_drift_environment}"
}

# Exactly what scripts/rbac-export.sh calls; no `*/read` wildcard (it would also cover resource types the export never
# touches, and future providers):
#   az role assignment list --all               -> roleAssignments/read, roleDefinitions/read (role names)
#   az identity list / federated-credential list -> userAssignedIdentities/read, federatedIdentityCredentials/read
#   az aks list -g                              -> managedClusters/read (kubelet identity)
#   resource group lookup                       -> resourceGroups/read
resource "azurerm_role_definition" "rbac_drift" {
  name        = "tidal-rbac-drift-reader"
  scope       = "/subscriptions/${var.subscription_id}"
  description = "T-408: read role assignments and the identities listed by scripts/rbac-export.sh. No write, no data actions."

  permissions {
    actions = [
      "Microsoft.Authorization/roleAssignments/read",
      "Microsoft.Authorization/roleDefinitions/read",
      "Microsoft.ManagedIdentity/userAssignedIdentities/read",
      "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/read",
      "Microsoft.ContainerService/managedClusters/read",
      "Microsoft.Resources/subscriptions/resourceGroups/read",
    ]
    not_actions = []
  }

  assignable_scopes = ["/subscriptions/${var.subscription_id}"]
}

# Subscription scope is required: the export lists assignments of known Tidal principals at ANY scope.
resource "azurerm_role_assignment" "rbac_drift" {
  scope              = "/subscriptions/${var.subscription_id}"
  role_definition_id = azurerm_role_definition.rbac_drift.role_definition_resource_id
  principal_id       = azuread_service_principal.rbac_drift.object_id
  description        = "T-408: nightly RBAC drift export"
}

resource "azuread_app_role_assignment" "rbac_drift_graph_application_read_all" {
  count               = var.grant_plan_graph_permission ? 1 : 0
  app_role_id         = data.azuread_service_principal.msgraph[0].app_role_ids["Application.Read.All"]
  principal_object_id = azuread_service_principal.rbac_drift.object_id
  resource_object_id  = data.azuread_service_principal.msgraph[0].object_id
}
