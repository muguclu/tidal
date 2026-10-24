output "api" {
  description = "tidal-api registration: client_id, identifier_uri (TIDAL_AUTH__AUDIENCE), object_id, service_principal_id, scope, default_scope and the app role GUIDs by role value."
  value = {
    client_id            = azuread_application.api.client_id
    identifier_uri       = var.api_identifier_uri
    object_id            = azuread_application.api.object_id
    service_principal_id = azuread_service_principal.api.object_id
    scope                = "${var.api_identifier_uri}/${local.scope_value}"
    default_scope        = "${var.api_identifier_uri}/.default"
    role_ids             = local.api_role_ids
  }
}

output "cli" {
  description = "tidal-cli public client: client_id, object_id and service_principal_id (device code flow, scope = api.scope)."
  value = {
    client_id            = azuread_application.cli.client_id
    object_id            = azuread_application.cli.object_id
    service_principal_id = azuread_service_principal.cli.object_id
  }
}

output "query" {
  description = "tidal-query registration: client_id, identifier_uri (TIDAL_QUERY__AUDIENCE), object_id, service_principal_id, default_scope and the Tidal.Query.Read role GUID and value."
  value = {
    client_id            = azuread_application.query.client_id
    identifier_uri       = var.query_identifier_uri
    object_id            = azuread_application.query.object_id
    service_principal_id = azuread_service_principal.query.object_id
    role_id              = local.query_role_id
    role_value           = local.query_role_value
    default_scope        = "${var.query_identifier_uri}/.default"
  }
}

output "api_role_values" {
  description = "App role values emitted in the roles claim of tidal-api tokens (x-tidal-min-role in openapi.yaml)."
  value       = sort(keys(local.api_roles))
}
