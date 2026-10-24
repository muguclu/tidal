# log_analytics

Log Analytics workspace (`PerGB2018`, daily cap 0.2 GB, 30 d retention; SPEC-07 section 2) and a
workspace-based Application Insights component.

- Cost control: `daily_quota_gb` (validated > 0, unlimited is not allowed) is the hard ceiling. Data above the cap is dropped until the daily reset, so alerting on the cap being hit belongs in the budget/observability tasks.
- Sampling: trace sampling (20 percent, 100 percent on errors) is SDK-side per SPEC-10 section 1. Do not lower the server-side `sampling_percentage` (default 100) to reach it; server-side sampling would stack on the SDK sampling and distort the counts.
- The workspace has local authentication disabled. Application Insights also has local authentication disabled (T-506): telemetry must be sent with an Entra token, so each publisher needs Monitoring Metrics Publisher on the App Insights resource. Public ingestion and query endpoints stay on (AMPLS skipped for cost; private networking is Phase 4).
- The connection string output is sensitive. It is kept because the SDK needs the ingestion endpoint from it (it carries no secret once local authentication is off) and envs/dev/rbac.tf stores it in Key Vault (`appinsights-connection-string`). Wiring it into the apps as a Key Vault reference is still open: the container_apps and app_service modules reject `*CONNECTION_STRING*` settings and have no secret support.

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |

## Resources

| Name | Type |
|------|------|
| `azurerm_log_analytics_workspace.this` | resource |
| `azurerm_application_insights.this` | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name; used in resource names. | `string` | `"dev"` | no |
| `resource_group_name` | Resource group that holds the workspace and Application Insights. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `daily_quota_gb` | Workspace daily ingestion cap in GB (must be > 0). | `number` | `0.2` | no |
| `retention_in_days` | Retention in days; one of 30, 60, 90, 120, 180, 270, 365, 550, 730. | `number` | `30` | no |
| `sampling_percentage` | Application Insights ingestion sampling percentage (0, 100]. | `number` | `100` | no |
| `tags` | Tag map from `module.resource_group.tags` (validated: six keys, park true/false, project tidal). | `map(string)` | n/a | yes |
| `component` | Component tag value, merged over `tags` in the module. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `workspace_id` | Log Analytics workspace resource ID. |
| `workspace_name` | Log Analytics workspace name. |
| `workspace_customer_id` | Workspace (customer) ID GUID. |
| `app_insights_id` | Application Insights resource ID. |
| `app_insights_connection_string` | Connection string (sensitive). |
<!-- END_TF_DOCS -->
