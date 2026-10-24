# Entra ID app registrations and roles (T-401, SPEC-06 section 1). Everything Entra-specific lives in this file.
# Needs a tenant role that can create applications and assign app roles (Application Administrator, or
# Application.ReadWrite.All + AppRoleAssignment.ReadWrite.All for the applying principal).

variable "entra_role_group_object_ids" {
  description = "Entra group object IDs per tidal-api app role, keyed Tidal.Reader, Tidal.Trader, Tidal.Quant, Tidal.Admin. Default empty: nobody is assigned until groups exist."
  type        = map(set(string))
  default     = {}
}

variable "entra_owner_object_ids" {
  description = "Object IDs that own the Tidal app registrations (add yourself, or an owners group)."
  type        = set(string)
  default     = []
}

variable "entra_query_read_include_worker" {
  description = "Also grant id-worker the Tidal.Query.Read role. SPEC-06 section 2 and ADR-0016 have the worker read through tidal-query, but T-401 grants only id-api; flip this when the worker path goes live."
  type        = bool
  default     = false
}

module "entra_app" {
  source = "../../modules/entra_app"

  tags             = module.resource_group.tags
  component        = "identity"
  owner_object_ids = var.entra_owner_object_ids

  role_group_object_ids = var.entra_role_group_object_ids

  query_read_principal_object_ids = merge(
    { "id-api" = module.identity.identities["id-api"].principal_id },
    var.entra_query_read_include_worker ? { "id-worker" = module.identity.identities["id-worker"].principal_id } : {}
  )
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
      TIDAL_AUTH__AUDIENCE  = module.entra_app.api.identifier_uri
      TIDAL_QUERY__AUDIENCE = module.entra_app.query.identifier_uri
    }
  }
}
