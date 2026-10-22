# sql

Azure SQL server and database `tidal` (hot store). GP serverless Gen5 `GP_S_Gen5_2` (min 0.5 / max 2 vCore),
auto-pause 60 min, 32 GB, zone redundancy off, Local backups (SPEC-07 section 2).

- Entra-only: `azuread_authentication_only = true`; there is no `administrator_login_password` anywhere. Pass an Entra group
  (recommended) as `entra_admin_login` / `entra_admin_object_id`. Workload identities are mapped in the database with
  `CREATE USER [id-api] FROM EXTERNAL PROVIDER` (T-403), no SQL logins (SPEC-06 section 2).
- TLS 1.2 minimum. `public_network_access_enabled` is `!enable_private_networking` (SPEC-06 section 4); the private endpoint belongs to the Phase 4 network module.
- Firewall: no rules by default. `allow_azure_services = true` adds the 0.0.0.0 "Allow Azure services" rule, which allows Azure IPs of any tenant to reach the server on TDS (Entra-only authentication still applies). Prefer specific IP rules until T-404 (private networking); use this only as a stopgap. A 0.0.0.0/0 rule is never created.
- Config contract: output `server_fqdn` is `TIDAL_SQL__SERVER`, `database_name` is `TIDAL_SQL__DATABASE`; the app uses `TIDAL_SQL__AUTH=entra_mi`.
- Free offer (LOCAL-DEV section 2, ADR-0011): `use_free_offer` (default true, dev-only lever; set false for prod or when the subscription already has 10 free databases) sets `useFreeLimit = var.use_free_offer` (and `freeLimitExhaustionBehavior = "AutoPause"` when on, omitted when off) through an `azapi_update_resource` (API `Microsoft.Sql/servers/databases@2025-01-01`), because azurerm 4.81.0 and 5.10.0 have no native arguments (upstream PRs 32055 and 32277). When they ship, set them on `azurerm_mssql_database`, delete the azapi resource and provider, and add `removed { from = azapi_update_resource.free_offer  lifecycle { destroy = false } }` (no recreate). The resource carries `replace_triggered_by = [azurerm_mssql_database.tidal]`; confirm that behaviour on the first dev apply. Caveat: the offer is normally chosen at database creation; acceptance as an update on a just-created database is unverified. If the first dev apply rejects it, switch the database to an `azapi_resource` with the same properties. Costs: `docs/finops/deltas/T-123.md`.
- `server_name_suffix` is required (3 to 8 lowercase alphanumerics) because the server name is globally unique; the env composition supplies it.
- SQL user mapping (`CREATE USER ... FROM EXTERNAL PROVIDER`) is left to T-403; this module only sets the Entra admin.
- Checkov skips (inline, with reasons): public access and private endpoint (Phase 4 flag), auditing and vulnerability assessment (T-407), zone redundancy (cost), ledger, optional 0.0.0.0 rule. The tflint `prevent_destroy` rule is disabled so dev environments can be destroyed.
- Out of scope here: TDE with a customer-managed key (service-managed TDE stays on).

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |
| azapi | ~> 2.0 |

## Resources

| Name | Type |
|------|------|
| `azurerm_mssql_server.this` | resource |
| `azurerm_mssql_database.tidal` | resource |
| `azurerm_mssql_firewall_rule.allow_azure_services` | resource (count 0 by default) |
| `azapi_update_resource.free_offer` | resource (always; reverts the offer when `use_free_offer = false`) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name (dev, prod). | `string` | `"dev"` | no |
| `resource_group_name` | Resource group for the server and database. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `server_name_suffix` | Required suffix (3 to 8 lowercase alphanumerics) for the globally unique server name. | `string` | n/a | yes |
| `tenant_id` | Entra tenant ID (GUID). | `string` | n/a | yes |
| `entra_admin_login` | Entra administrator display name (group recommended). | `string` | n/a | yes |
| `entra_admin_object_id` | Entra administrator object ID (GUID). | `string` | n/a | yes |
| `enable_system_assigned_identity` | System-assigned identity on the server. | `bool` | `false` | no |
| `enable_private_networking` | Phase 4 flag; disables the public endpoint. | `bool` | `false` | no |
| `allow_azure_services` | Create the 0.0.0.0 Allow Azure services rule. | `bool` | `false` | no |
| `use_free_offer` | Apply the free offer via azapi (ADR-0011). | `bool` | `true` | no |
| `database_name` | Database name. | `string` | `"tidal"` | no |
| `sku_name` | Serverless GP Gen5 SKU (GP_S_Gen5_1, _2, _4). | `string` | `"GP_S_Gen5_2"` | no |
| `min_capacity` | Minimum vCores while active. | `number` | `0.5` | no |
| `auto_pause_delay_in_minutes` | Idle minutes before auto-pause (15 to 10080). | `number` | `60` | no |
| `max_size_gb` | Maximum size in GB. | `number` | `32` | no |
| `tags` | Tag map from `module.resource_group.tags` (validated). | `map(string)` | n/a | yes |
| `component` | Component tag value, merged over `tags`. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `server_id` | SQL server resource ID. |
| `server_name` | SQL server name. |
| `server_fqdn` | Server FQDN (`TIDAL_SQL__SERVER`). |
| `database_id` | Database resource ID. |
| `database_name` | Database name (`TIDAL_SQL__DATABASE`). |
| `server_principal_id` | System-assigned identity principal ID, or null. |
<!-- END_TF_DOCS -->
