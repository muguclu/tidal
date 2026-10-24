# Entra ID registrations for Tidal (SPEC-06 section 1, T-401).
#   tidal-api    api://tidal         app roles Tidal.Reader/Trader/Quant/Admin (assigned to groups) + delegated scope
#   tidal-cli    public client       device code, delegated access_as_user on tidal-api
#   tidal-query  api://tidal-query   app role Tidal.Query.Read (application-only), assigned only to listed managed identities
# Entra app registrations and service principals are free (docs/finops/deltas/T-401.md).

locals {
  tags = [for k, v in merge(var.tags, { component = var.component }) : "${k}:${v}"]

  api_roles = {
    "Tidal.Reader" = "Read market data and own backtest runs."
    "Tidal.Trader" = "Reader plus trader-level endpoints."
    "Tidal.Quant"  = "Trader plus submit and manage backtests."
    "Tidal.Admin"  = "Full access including administrative endpoints."
  }

  # Stable GUIDs: uuidv5 in the URL namespace over "<identifier URI>/<role value>". Never rename a role value or
  # change an identifier URI: the GUID would change and existing assignments and issued tokens would break.
  api_role_ids = { for r, _ in local.api_roles : r => uuidv5("url", "${var.api_identifier_uri}/${r}") }

  query_role_value = "Tidal.Query.Read"
  query_role_id    = uuidv5("url", "${var.query_identifier_uri}/${local.query_role_value}")

  scope_value = "access_as_user"
  scope_id    = uuidv5("url", "${var.api_identifier_uri}/scope/${local.scope_value}")

  # One assignment per (role, group).
  group_assignments = merge([
    for role, ids in var.role_group_object_ids : {
      for id in ids : "${role}/${id}" => { role = role, group = id }
    }
  ]...)
}

# --- tidal-api --------------------------------------------------------------------------------

resource "azuread_application" "api" {
  display_name     = "tidal-api"
  identifier_uris  = [var.api_identifier_uri]
  sign_in_audience = "AzureADMyOrg"
  owners           = var.owner_object_ids
  tags             = local.tags
  description      = "Tidal public API (App Service). Roles arrive in the roles claim."

  api {
    # v2 tokens: issuer https://login.microsoftonline.com/<tenant>/v2.0 and aud = this application's client ID.
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = local.scope_id
      value                      = local.scope_value
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Access Tidal as the signed-in user"
      admin_consent_description  = "Allows the application to call the Tidal API on behalf of the signed-in user. The user's Tidal app roles still apply."
      user_consent_display_name  = "Access Tidal as you"
      user_consent_description   = "Allows the application to call the Tidal API on your behalf. Your Tidal roles still apply."
    }
  }

  dynamic "app_role" {
    for_each = local.api_roles
    content {
      id                   = local.api_role_ids[app_role.key]
      value                = app_role.key
      display_name         = app_role.key
      description          = app_role.value
      allowed_member_types = ["User"] # assignment to groups uses the "User" member type
      enabled              = true
    }
  }
}

resource "azuread_service_principal" "api" {
  client_id                    = azuread_application.api.client_id
  app_role_assignment_required = true # only assigned groups get a token
  owners                       = var.owner_object_ids
  tags                         = local.tags
}

resource "azuread_app_role_assignment" "api_group" {
  for_each = local.group_assignments

  app_role_id         = local.api_role_ids[each.value.role]
  principal_object_id = each.value.group
  resource_object_id  = azuread_service_principal.api.object_id
}

# --- tidal-cli --------------------------------------------------------------------------------

resource "azuread_application" "cli" {
  display_name                   = "tidal-cli"
  sign_in_audience               = "AzureADMyOrg"
  owners                         = var.owner_object_ids
  tags                           = local.tags
  description                    = "Tidal demo CLI (public client, device code flow)."
  fallback_public_client_enabled = true # required for the device code flow; no secret or certificate exists

  required_resource_access {
    resource_app_id = azuread_application.api.client_id

    resource_access {
      id   = local.scope_id
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "cli" {
  client_id = azuread_application.cli.client_id
  owners    = var.owner_object_ids
  tags      = local.tags
}

# Pre-authorise the CLI so users are not asked for consent to access_as_user.
resource "azuread_application_pre_authorized" "cli" {
  application_id       = azuread_application.api.id
  authorized_client_id = azuread_application.cli.client_id
  permission_ids       = [local.scope_id]
}

# --- tidal-query ------------------------------------------------------------------------------

resource "azuread_application" "query" {
  display_name     = "tidal-query"
  identifier_uris  = [var.query_identifier_uri]
  sign_in_audience = "AzureADMyOrg"
  owners           = var.owner_object_ids
  tags             = local.tags
  description      = "Tidal internal query service (Container Apps). Callable only by managed identities holding Tidal.Query.Read."

  api {
    requested_access_token_version = 2
  }

  app_role {
    id                   = local.query_role_id
    value                = local.query_role_value
    display_name         = local.query_role_value
    description          = "Read market data through tidal-query (service to service)."
    allowed_member_types = ["Application"]
    enabled              = true
  }
}

resource "azuread_service_principal" "query" {
  client_id                    = azuread_application.query.client_id
  app_role_assignment_required = true # a token is only issued to principals holding the role
  owners                       = var.owner_object_ids
  tags                         = local.tags
}

resource "azuread_app_role_assignment" "query_read" {
  for_each = var.query_read_principal_object_ids

  app_role_id         = local.query_role_id
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.query.object_id
}
