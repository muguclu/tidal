# monitoring

Azure Workbook (SPEC-10 section 2) and the SPEC-10 section 3 alerts, plus activity-log and blob audit alerts that close
ADR-0008 R-1. It reuses the email action group of `modules/budget`, Application Insights and the workspace of
`modules/log_analytics`; it creates no action group and no budget.

## Alerts

| Alert | Kind | Threshold | Source |
|---|---|---|---|
| `alert-tidal-api-5xx-<env>` | log, 5 min | 5xx share above 2 % over 10 min, at least 20 requests | App Insights `requests` |
| `alert-tidal-dlq-<env>` | metric, 5 min | `DeadletteredMessages` above 0, per entity | Service Bus namespace |
| `alert-tidal-ingest-freshness-<env>` | log, hourly, disabled by default (`enable_ingest_freshness_alert`) | no positive `tidal.ingest.rows_written` datapoint for 36 h, UTC Mon to Fri | workspace `AppMetrics` |
| `alert-tidal-la-daily-cap-<env>` | log, 15 min | "Data collection stopped" / `OverQuota` in the operations table | workspace `_LogOperation` |
| `alert-tidal-spot-eviction-<env>` | log, 15 min | above 30 % of spot nodes gone in 1 h without autoscaler scale-down | `KubeNodeInventory`, `KubeEvents`; only with `container_insights_enabled` |
| budget 80 % | - | already in `modules/budget` (actual 80 % notification) | Cost Management |
| `alert-tidal-mgmt-policy-<env>` | activity log | `managementPolicies/write` on the storage account | R-1 |
| `alert-tidal-role-write-<env>`, `alert-tidal-role-delete-<env>` | activity log | `roleAssignments/write` and `/delete` in the resource group | R-1 |
| `alert-tidal-blob-audit-<env>` | log, 15 min | successful PutBlob, PutBlockList, SetBlobTier, DeleteContainer, UndeleteBlob, SetBlobTags or DeleteBlob on `marketdata` by shared key, SAS, or an OAuth caller outside `expected_blob_modifier_object_ids` | `StorageBlobLogs` via a diagnostic setting |
| `alert-tidal-<control>-<env>` x17 | activity log | storage account write/delete/listKeys, blobServices write, immutability policy write/delete/lock, diagnostic setting, scheduled query / activity log / metric alert and action group write/delete, all at resource group scope | R-1 bypass controls |

Limits that are real, not hidden:

- **Freshness is a proxy.** The metric has no instrument attribute (telemetry.py keeps cardinality low), so this measures the age of
  the last ingest write, not the age of the latest bar per instrument. The ingest job runs daily, so a missing datapoint means a
  missed run.
- **Spot eviction is a proxy.** Node disappearance without an autoscaler ScaleDown event; dev has Container Insights off, so the rule
  is not created there. Enabling it is a cost decision in `modules/aks`.
- **Activity-log alerts cannot exclude a caller** (the criteria have no negation), so Terraform applies fire them. They are expected
  during an apply window and not otherwise. Data-plane blob calls are not in the activity log, which is why the blob audit uses
  diagnostic logs and can exclude the expected principals. Service-internal operations (lifecycle management) carry no requester
  object id and are ignored by that rule.
- Alert queries and the workbook are NEEDS-CLOUD: nothing here could be run against live data.

## Workbook sections

1. Platform health: API p50/p95/p99, error rate, dependency failures, queue depth, DLQ count, queue lag, cold start.
2. Batch: KEDA replicas, nodes per pool, spot evictions (Container Insights tiles stay empty while it is off), backtest duration.
3. Data: rows per tier, bytes scanned, rows moved, Blob capacity per tier, SQL space, ingest freshness per source.
4. Cost: query and backtest volumes (real); cost by `component` tag and EUR per backtest / per 1k queries are labelled PLACEHOLDER
   until the T-502 export is queryable from Log Analytics or ADX.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |

## Resources

| Name | Type |
|------|------|
| `azurerm_application_insights_workbook.tidal` | resource |
| `azurerm_monitor_scheduled_query_rules_alert_v2.api_5xx` | resource |
| `azurerm_monitor_scheduled_query_rules_alert_v2.ingest_freshness` | resource |
| `azurerm_monitor_scheduled_query_rules_alert_v2.la_daily_cap` | resource |
| `azurerm_monitor_scheduled_query_rules_alert_v2.spot_eviction` | resource (when `container_insights_enabled`) |
| `azurerm_monitor_scheduled_query_rules_alert_v2.blob_audit` | resource (when `enable_blob_audit`) |
| `azurerm_monitor_diagnostic_setting.blob_audit` | resource (when `enable_blob_audit`) |
| `azurerm_monitor_metric_alert.dlq` | resource |
| `azurerm_monitor_activity_log_alert.control_plane` | resource (one per entry: mgmt-policy, role-write, role-delete and 17 watched operations) |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name (dev, prod). | `string` | `"dev"` | no |
| `resource_group_name` | Resource group of the workbook and rules. | `string` | n/a | yes |
| `resource_group_id` | Resource group ID; scope of the role-assignment alerts. | `string` | n/a | yes |
| `location` | Region of the workbook and scheduled query rules. | `string` | n/a | yes |
| `action_group_id` | Existing action group (modules/budget). | `string` | n/a | yes |
| `app_insights_id` | Application Insights ID. | `string` | n/a | yes |
| `workspace_id` | Log Analytics workspace ID. | `string` | n/a | yes |
| `servicebus_namespace_id` | Service Bus namespace ID. | `string` | n/a | yes |
| `storage_account_id` | Storage account ID. | `string` | n/a | yes |
| `sql_database_id` | SQL database ID for the space tile; null omits it. | `string` | `null` | no |
| `enable_ingest_freshness_alert` | Enable the freshness rule once ingest emits telemetry. | `bool` | `false` | no |
| `container_insights_enabled` | Container Insights tables exist; creates the spot eviction alert. | `bool` | `false` | no |
| `enable_blob_audit` | Blob diagnostic logs plus the unexpected-caller alert. | `bool` | `true` | no |
| `expected_blob_modifier_object_ids` | Principals allowed to set blob tags or delete blobs. | `list(string)` | `[]` | no |
| `api_5xx_threshold_percent` | 5xx share threshold. | `number` | `2` | no |
| `api_5xx_min_requests` | Minimum requests before the ratio counts. | `number` | `20` | no |
| `ingest_freshness_hours` | Hours without ingest on a weekday. | `number` | `36` | no |
| `spot_eviction_threshold_percent` | Evicted share of spot nodes per hour. | `number` | `30` | no |
| `tags` | Tag map from `module.resource_group.tags`. | `map(string)` | n/a | yes |
| `component` | `component` tag value. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `workbook_id` | Workbook resource ID. |
| `alert_rule_ids` | Alert name to resource ID. |
<!-- END_TF_DOCS -->
