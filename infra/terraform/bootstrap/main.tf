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

# No pull_request credential on purpose: a PR token would carry every role of sp-tidal-ci and bypass the
# `environment: dev` protection. T-127 adds a separate app sp-tidal-ci-plan (Reader + Storage Blob Data Reader only).

# ---------------------------------------------------------------------------
# RBAC: state container only. AcrPush / Contributor on rg-tidal-dev are added in later tasks (SPEC-06 section 2).
# ---------------------------------------------------------------------------
resource "azurerm_role_assignment" "ci_state_blob" {
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.ci.object_id
  principal_type       = "ServicePrincipal"
}
