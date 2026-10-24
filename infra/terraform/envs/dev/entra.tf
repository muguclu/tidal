# Entra ID app registrations and roles (T-401, SPEC-06 section 1). Everything Entra-specific lives in this file.
# Needs a tenant role that can create applications and assign app roles (Application Administrator, or
# Application.ReadWrite.All + AppRoleAssignment.ReadWrite.All for the applying principal).

variable "entra_role_group_object_ids" {
  description = "Entra group object IDs per tidal-api app role, keyed Tidal.Reader, Tidal.Trader, Tidal.Quant, Tidal.Admin. Default empty: nobody is assigned until groups exist."
  type        = map(set(string))
  default     = {}
}

variable "entra_owner_object_ids" {
  description = "Object IDs that own the Tidal app registrations. Keep it empty, or use one named admin or a PIM-eligible group; never sp-tidal-ci. Owners can add app role assignments or credentials outside Terraform; the T-403 drift export covers appRoleAssignedTo and the credentials, so such changes show up there."
  type        = set(string)
  default     = []
}

variable "entra_query_read_include_worker" {
  description = "Also grant id-worker the Tidal.Query.Read role (ADR-0016: the worker reads market data through tidal-query, never SQL). id-api always gets it. Set false only to run without the worker read path."
  type        = bool
  default     = true
}

locals {
  # Managed identities allowed to call tidal-query (same set the role assignments use).
  query_callers = merge(
    { "id-api" = module.identity.identities["id-api"] },
    var.entra_query_read_include_worker ? { "id-worker" = module.identity.identities["id-worker"] } : {}
  )
}

module "entra_app" {
  source = "../../modules/entra_app"

  tags             = module.resource_group.tags
  component        = "identity"
  owner_object_ids = var.entra_owner_object_ids

  role_group_object_ids = var.entra_role_group_object_ids

  query_read_principal_object_ids = { for name, i in local.query_callers : name => i.principal_id }
}

output "entra" {
  description = "Entra registrations for T-402 (JWT validation), T-403 and the deploy config: tenant, issuer, audiences, client IDs, role GUIDs."
  value = {
    tenant_id = data.azurerm_client_config.current.tenant_id
    issuer    = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
    jwks_uri  = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/discovery/v2.0/keys"
    api       = module.entra_app.api
    cli       = module.entra_app.cli
    query     = module.entra_app.query
    config = {
      TIDAL_AUTH__AUDIENCE                = module.entra_app.api.identifier_uri
      TIDAL_AUTH__API_APP_CLIENT_ID       = module.entra_app.api.client_id
      TIDAL_AUTH__ALLOWED_CLIENT_IDS      = module.entra_app.cli.client_id
      TIDAL_QUERY__AUDIENCE               = module.entra_app.query.identifier_uri
      TIDAL_QUERY__APP_CLIENT_ID          = module.entra_app.query.client_id
      TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS = join(",", [for name in sort(keys(local.query_callers)) : local.query_callers[name].client_id])
      TIDAL_AUTH__QUERY_CALLER_OBJECT_IDS = join(",", [for name in sort(keys(local.query_callers)) : local.query_callers[name].principal_id])
    }
  }
}
