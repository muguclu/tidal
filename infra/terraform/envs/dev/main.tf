data "azurerm_client_config" "current" {}

locals {
  env = "dev"
}

# --- Foundation -------------------------------------------------------------------------------

module "resource_group" {
  source = "../../modules/resource_group"

  env      = local.env
  location = var.location
  owner    = var.owner
  park     = var.park
}

module "log_analytics" {
  source = "../../modules/log_analytics"

  env                 = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "observability"
}

module "identity" {
  source = "../../modules/identity"

  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "identity"
}

# --- Data and messaging -----------------------------------------------------------------------

module "storage" {
  source = "../../modules/storage"

  name                      = "sttidal${local.env}${var.name_suffix}"
  resource_group_name       = module.resource_group.name
  location                  = module.resource_group.location
  enable_private_networking = var.enable_private_networking
  tags                      = module.resource_group.tags
  component                 = "storage"
}

module "key_vault" {
  source = "../../modules/key_vault"

  name                      = "kv-tidal-${local.env}-${var.name_suffix}"
  resource_group_name       = module.resource_group.name
  location                  = module.resource_group.location
  enable_private_networking = var.enable_private_networking
  tags                      = module.resource_group.tags
  component                 = "secrets"
}

module "acr" {
  source = "../../modules/acr"

  env                 = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  name_suffix         = var.name_suffix
  tags                = module.resource_group.tags
  component           = "registry"
}

module "sql" {
  source = "../../modules/sql"

  env                        = local.env
  resource_group_name        = module.resource_group.name
  location                   = module.resource_group.location
  server_name_suffix         = var.name_suffix
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  entra_admin_login          = var.sql_entra_admin_login
  entra_admin_object_id      = var.sql_entra_admin_object_id
  enable_private_networking  = var.enable_private_networking
  allowed_ip_addresses       = local.sql_allowed_ip_addresses
  use_free_offer             = var.use_free_offer
  log_analytics_workspace_id = module.log_analytics.workspace_id
  tags                       = module.resource_group.tags
  component                  = "data"
}

module "servicebus" {
  source = "../../modules/servicebus"

  env                 = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  name_suffix         = var.name_suffix
  enable_ingest_topic = var.enable_ingest_topic
  tags                = module.resource_group.tags
  component           = "messaging"
}

# --- FinOps -----------------------------------------------------------------------------------

module "budget" {
  source = "../../modules/budget"

  env                  = local.env
  resource_group_name  = module.resource_group.name
  resource_group_id    = module.resource_group.id
  alert_emails         = var.alert_emails
  budget_scope         = var.budget_scope
  start_date           = var.budget_start_date
  enable_anomaly_alert = var.enable_anomaly_alert
  tags                 = module.resource_group.tags
  component            = "finops"
}

# --- Not composed yet -------------------------------------------------------------------------
# The modules below do not exist yet and are wired in by later tasks:
#   network (private networking flag)  added in T-404
#   aks                                added in T-306
#   app_service, container_apps        added in T-309
#   vm_legacy (+ Logic App schedule)   added in T-310
# Role assignments (SPEC-06 section 2) are added with those consumers and with T-401/T-403.
