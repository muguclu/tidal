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
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0 |
| <a name="requirement_azapi"></a> [azapi](#requirement\_azapi) | ~> 2.0 |
| <a name="requirement_azurerm"></a> [azurerm](#requirement\_azurerm) | ~> 4.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_azapi"></a> [azapi](#provider\_azapi) | ~> 2.0 |
| <a name="provider_azurerm"></a> [azurerm](#provider\_azurerm) | ~> 4.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [azapi_resource.va_express](https://registry.terraform.io/providers/Azure/azapi/latest/docs/resources/resource) | resource |
| [azapi_update_resource.audit_action_groups](https://registry.terraform.io/providers/Azure/azapi/latest/docs/resources/update_resource) | resource |
| [azapi_update_resource.free_offer](https://registry.terraform.io/providers/Azure/azapi/latest/docs/resources/update_resource) | resource |
| [azurerm_monitor_diagnostic_setting.sql_audit](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/monitor_diagnostic_setting) | resource |
| [azurerm_mssql_database.tidal](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/mssql_database) | resource |
| [azurerm_mssql_firewall_rule.allow_azure_services](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/mssql_firewall_rule) | resource |
| [azurerm_mssql_server.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/mssql_server) | resource |
| [azurerm_mssql_server_extended_auditing_policy.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/mssql_server_extended_auditing_policy) | resource |
| [azurerm_mssql_server_security_alert_policy.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/mssql_server_security_alert_policy) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_allow_azure_services"></a> [allow\_azure\_services](#input\_allow\_azure\_services) | Create the 0.0.0.0 Allow Azure services firewall rule. It allows Azure IPs of any tenant to reach the server on TDS (Entra-only authentication still applies), so it is off by default. Prefer specific IP rules until T-404 (private networking). Ignored when enable\_private\_networking is true. A 0.0.0.0/0 public rule is never created. | `bool` | `false` | no |
| <a name="input_audit_action_groups"></a> [audit\_action\_groups](#input\_audit\_action\_groups) | Server audit action groups. Dev default is low volume: failed logins and security-relevant changes. Add SUCCESSFUL\_DATABASE\_AUTHENTICATION\_GROUP or BATCH\_COMPLETED\_GROUP only with a daily-cap check (docs/finops/deltas/T-407\_model.py), because the workspace cap is 0.2 GB/day. | `list(string)` | <pre>[<br/>  "FAILED_DATABASE_AUTHENTICATION_GROUP",<br/>  "DATABASE_PRINCIPAL_CHANGE_GROUP",<br/>  "DATABASE_ROLE_MEMBER_CHANGE_GROUP",<br/>  "DATABASE_PERMISSION_CHANGE_GROUP",<br/>  "SCHEMA_OBJECT_CHANGE_GROUP"<br/>]</pre> | no |
| <a name="input_auto_pause_delay_in_minutes"></a> [auto\_pause\_delay\_in\_minutes](#input\_auto\_pause\_delay\_in\_minutes) | Idle minutes before auto-pause (SPEC-07: 60). -1 disables pausing and is deliberately not allowed; the minimum accepted by Azure is 15, SPEC-07 sets 60. | `number` | `60` | no |
| <a name="input_component"></a> [component](#input\_component) | Value of the component tag for the resources in this module (for example data); merged over tags["component"] inside the module. | `string` | n/a | yes |
| <a name="input_database_name"></a> [database\_name](#input\_database\_name) | Database name (config-contract TIDAL\_SQL\_\_DATABASE default: tidal). | `string` | `"tidal"` | no |
| <a name="input_enable_defender_for_sql"></a> [enable\_defender\_for\_sql](#input\_enable\_defender\_for\_sql) | Enable Microsoft Defender for SQL on this server (13.20 EUR/month per server, billed while the server exists, not reduced by auto-pause). Off by default per ADR-0017. | `bool` | `false` | no |
| <a name="input_enable_private_networking"></a> [enable\_private\_networking](#input\_enable\_private\_networking) | Phase 4 private networking flag (SPEC-06 section 4). When true the public endpoint is disabled and no firewall rule is created; the private endpoint itself is owned by the network module. | `bool` | `false` | no |
| <a name="input_enable_system_assigned_identity"></a> [enable\_system\_assigned\_identity](#input\_enable\_system\_assigned\_identity) | Give the server a system-assigned identity (needed later for TDE with a customer-managed key or auditing to storage). | `bool` | `false` | no |
| <a name="input_enable_vulnerability_assessment"></a> [enable\_vulnerability\_assessment](#input\_enable\_vulnerability\_assessment) | Enable SQL Vulnerability Assessment (Express configuration, no storage account). Only effective when enable\_defender\_for\_sql is true. Off by default per ADR-0017. | `bool` | `false` | no |
| <a name="input_entra_admin_login"></a> [entra\_admin\_login](#input\_entra\_admin\_login) | Display name of the Entra administrator. An Entra group is recommended (membership changes need no Terraform run); a single user works too. Never a SQL login. | `string` | n/a | yes |
| <a name="input_entra_admin_object_id"></a> [entra\_admin\_object\_id](#input\_entra\_admin\_object\_id) | Object ID of the Entra administrator (group recommended). | `string` | n/a | yes |
| <a name="input_env"></a> [env](#input\_env) | Environment name; used in the server name (sql-tidal-<env>). | `string` | `"dev"` | no |
| <a name="input_location"></a> [location](#input\_location) | Azure region. | `string` | `"westeurope"` | no |
| <a name="input_log_analytics_workspace_id"></a> [log\_analytics\_workspace\_id](#input\_log\_analytics\_workspace\_id) | Resource ID of the Log Analytics workspace that receives the SQL audit events (module log\_analytics output workspace\_id). null disables server auditing (only for isolated module tests). | `string` | `null` | no |
| <a name="input_max_size_gb"></a> [max\_size\_gb](#input\_max\_size\_gb) | Maximum database size in GB (SPEC-07: 32, which is also the free offer storage limit). | `number` | `32` | no |
| <a name="input_min_capacity"></a> [min\_capacity](#input\_min\_capacity) | Minimum vCores while active (SPEC-07: 0.5). Must not exceed the SKU vCore count. | `number` | `0.5` | no |
| <a name="input_resource_group_name"></a> [resource\_group\_name](#input\_resource\_group\_name) | Resource group that holds the SQL server and database. | `string` | n/a | yes |
| <a name="input_server_name_suffix"></a> [server\_name\_suffix](#input\_server\_name\_suffix) | Suffix that makes the server name globally unique (sql-tidal-<env>-<suffix>); supplied by the env composition. | `string` | n/a | yes |
| <a name="input_sku_name"></a> [sku\_name](#input\_sku\_name) | Database SKU. SPEC-07 section 2: GP serverless Gen5 with max 2 vCore, i.e. GP\_S\_Gen5\_2. Only serverless GP Gen5 names are accepted. | `string` | `"GP_S_Gen5_2"` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tag map from module.resource\_group.tags. Must contain project, env, owner, cost\_center, component, park (non-empty), park must be true or false, project must be tidal. The component tag is overridden per resource from var.component. | `map(string)` | n/a | yes |
| <a name="input_tenant_id"></a> [tenant\_id](#input\_tenant\_id) | Entra tenant ID of the administrator. | `string` | n/a | yes |
| <a name="input_use_free_offer"></a> [use\_free\_offer](#input\_use\_free\_offer) | Apply the Azure SQL free offer (100,000 vCore-seconds and 32 GB per month, AutoPause at the limit) through azapi (ADR-0011). Dev-only cost lever; set false for prod or when the subscription already has 10 free databases. | `bool` | `true` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_auditing_enabled"></a> [auditing\_enabled](#output\_auditing\_enabled) | True when server auditing to Log Analytics is configured. |
| <a name="output_database_id"></a> [database\_id](#output\_database\_id) | Database resource ID. |
| <a name="output_database_name"></a> [database\_name](#output\_database\_name) | Database name. Maps to config-contract TIDAL\_SQL\_\_DATABASE. |
| <a name="output_server_fqdn"></a> [server\_fqdn](#output\_server\_fqdn) | SQL server FQDN. Maps to config-contract TIDAL\_SQL\_\_SERVER. |
| <a name="output_server_id"></a> [server\_id](#output\_server\_id) | SQL server resource ID. |
| <a name="output_server_name"></a> [server\_name](#output\_server\_name) | SQL server name. |
| <a name="output_server_principal_id"></a> [server\_principal\_id](#output\_server\_principal\_id) | Principal ID of the server system-assigned identity, or null when disabled. |
<!-- END_TF_DOCS -->
