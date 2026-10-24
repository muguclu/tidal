# entra_app

Entra ID registrations for Tidal (SPEC-06 section 1, T-401), managed with the `azuread` provider. No secrets, certificates or client secrets are created anywhere.

| Registration | Identifier URI | Purpose |
|---|---|---|
| `tidal-api` | `api://tidal` (`TIDAL_AUTH__AUDIENCE`) | Public API. App roles `Tidal.Reader`, `Tidal.Trader`, `Tidal.Quant`, `Tidal.Admin` (the `x-tidal-min-role` values), assigned to **groups**. Delegated scope `access_as_user`. `requested_access_token_version = 2`. |
| `tidal-cli` | none (public client) | Demo CLI, device code flow (`fallback_public_client_enabled`), pre-authorised for `access_as_user`. |
| `tidal-query` | `api://tidal-query` (`TIDAL_QUERY__AUDIENCE`) | Internal service. Application-only app role `Tidal.Query.Read`, assigned only to the managed identities in `query_read_principal_object_ids` (dev: `id-api`). |

- Service principals of `tidal-api` and `tidal-query` have `app_role_assignment_required = true`: a token is only issued to assigned groups or identities.
- Role and scope GUIDs are deterministic: `uuidv5("url", "<identifier URI>/<role value>")` (scope: `<URI>/scope/access_as_user`). Do not rename a role or change a URI after the first apply: assignments and tokens would break. Values are in the `api.role_ids` and `query.role_id` outputs.
- Token shape for validators (T-402): issuer `https://login.microsoftonline.com/<tenant>/v2.0`; v2 tokens carry `aud` = the application **client ID** GUID (not the `api://` URI), so accept both; user roles in `roles`, caller app in `azp`, caller object in `oid` (managed identity tokens: `roles` holds `Tidal.Query.Read`, `oid` is the identity principal ID, `azp` its client ID).
- Group assignment: `role_group_object_ids = { "Tidal.Reader" = ["<group object id>"] }`; default empty.
- Entra tags are a set of `key:value` strings built from the mandatory tag map.
- Cost: free (docs/finops/deltas/T-401.md). Applying needs Application Administrator (or Graph `Application.ReadWrite.All` and `AppRoleAssignment.ReadWrite.All`).

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0 |
| <a name="requirement_azuread"></a> [azuread](#requirement\_azuread) | ~> 3.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_azuread"></a> [azuread](#provider\_azuread) | ~> 3.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [azuread_app_role_assignment.api_group](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/app_role_assignment) | resource |
| [azuread_app_role_assignment.query_read](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/app_role_assignment) | resource |
| [azuread_application.api](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/application) | resource |
| [azuread_application.cli](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/application) | resource |
| [azuread_application.query](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/application) | resource |
| [azuread_application_pre_authorized.cli](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/application_pre_authorized) | resource |
| [azuread_service_principal.api](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/service_principal) | resource |
| [azuread_service_principal.cli](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/service_principal) | resource |
| [azuread_service_principal.query](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/service_principal) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_api_identifier_uri"></a> [api\_identifier\_uri](#input\_api\_identifier\_uri) | Identifier URI of the tidal-api registration. SPEC-06 section 1 fixes api://tidal; it is TIDAL\_AUTH\_\_AUDIENCE. | `string` | `"api://tidal"` | no |
| <a name="input_component"></a> [component](#input\_component) | Value of the component tag for the resources in this module (for example identity); merged over tags["component"]. | `string` | n/a | yes |
| <a name="input_owner_object_ids"></a> [owner\_object\_ids](#input\_owner\_object\_ids) | Object IDs that own the three applications and service principals (users, groups or service principals). The azuread provider does not add the caller itself; leave empty only if a tenant admin manages them. | `set(string)` | `[]` | no |
| <a name="input_query_identifier_uri"></a> [query\_identifier\_uri](#input\_query\_identifier\_uri) | Identifier URI of the tidal-query registration; it is TIDAL\_QUERY\_\_AUDIENCE (config default api://tidal-query). | `string` | `"api://tidal-query"` | no |
| <a name="input_query_read_principal_object_ids"></a> [query\_read\_principal\_object\_ids](#input\_query\_read\_principal\_object\_ids) | Service principal object IDs (managed identity principal\_id) granted the Tidal.Query.Read app role on tidal-query, keyed by a static label such as id-api. SPEC-06 and T-402: only id-api by default; add id-worker only when ADR-0016 worker reads go live. Keys must be known at plan time, values may be computed. | `map(string)` | `{}` | no |
| <a name="input_role_group_object_ids"></a> [role\_group\_object\_ids](#input\_role\_group\_object\_ids) | Entra group object IDs assigned to each tidal-api app role, keyed by role value (Tidal.Reader, Tidal.Trader, Tidal.Quant, Tidal.Admin). Roles go to groups, not users (SPEC-06 section 1). Missing keys or empty sets assign nobody. Group IDs must be known at plan time. | `map(set(string))` | `{}` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tag map from module.resource\_group.tags. Must contain project, env, owner, cost\_center, component, park (non-empty), park must be true or false, project must be tidal. Entra applications only carry a set of strings, so each pair is rendered as "key:value". | `map(string)` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_api"></a> [api](#output\_api) | tidal-api registration: client\_id, identifier\_uri (TIDAL\_AUTH\_\_AUDIENCE), object\_id, service\_principal\_id, scope, default\_scope and the app role GUIDs by role value. |
| <a name="output_api_role_values"></a> [api\_role\_values](#output\_api\_role\_values) | App role values emitted in the roles claim of tidal-api tokens (x-tidal-min-role in openapi.yaml). |
| <a name="output_cli"></a> [cli](#output\_cli) | tidal-cli public client: client\_id, object\_id and service\_principal\_id (device code flow, scope = api.scope). |
| <a name="output_query"></a> [query](#output\_query) | tidal-query registration: client\_id, identifier\_uri (TIDAL\_QUERY\_\_AUDIENCE), object\_id, service\_principal\_id, default\_scope and the Tidal.Query.Read role GUID and value. |
<!-- END_TF_DOCS -->
