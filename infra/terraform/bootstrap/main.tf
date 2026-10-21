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
  #checkov:skip=CKV_AZURE_59:Public network access stays enabled so GitHub-hosted runners and the human applier can reach it; protected by Entra-only auth (shared key disabled)
  #checkov:skip=CKV2_AZURE_33:No private endpoint in dev; private networking is a Phase 4 flag
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
}

resource "azurerm_storage_container" "tfstate" {
  #checkov:skip=CKV2_AZURE_21:Blob read logging needs Log Analytics, which does not exist at bootstrap; revisit with the log_analytics module
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
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

# Pull-request jobs (read-only terraform plan later, T-127). Roles granted to this identity for PRs must stay read-only.
resource "azuread_application_federated_identity_credential" "github_pull_request" {
  application_id = azuread_application.ci.id
  display_name   = "github-pull-request"
  description    = "GitHub Actions pull_request workflows (read-only plan)"
  issuer         = local.github_issuer
  audiences      = local.github_audiences
  subject        = "repo:${local.github_repo_path}:pull_request"
}

# ---------------------------------------------------------------------------
# RBAC: state container only. AcrPush / Contributor on rg-tidal-dev are added in later tasks (SPEC-06 section 2).
# ---------------------------------------------------------------------------
resource "azurerm_role_assignment" "ci_state_blob" {
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.ci.object_id
  principal_type       = "ServicePrincipal"
}
