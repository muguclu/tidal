locals {
  tags = merge(var.tags, { component = var.component })

  # Rule names follow alert-tidal-<what>-<env>. All rules notify the single action group of modules/budget.
  action_group_ids = [var.action_group_id]

  # Query kinds (v2 scheduled query rules): fire when the query returns at least one row.
  row_count_criteria = {
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0
  }

  blob_expected_callers = jsonencode(var.expected_blob_modifier_object_ids)

  # --- KQL (kept here so the README can describe them and tests can read them) ---------------

  # SPEC-10 section 3: API 5xx > 2 % for 10 min. The window is the 10 minutes; the ratio is guarded by a minimum request
  # count because dev traffic is near zero. Source: Application Insights `requests` (classic table names, resource scope).
  kql_api_5xx = <<-EOT
    requests
    | where timestamp > ago(10m)
    | summarize total = count(), errors = countif(toint(resultCode) >= 500)
    | extend error_pct = 100.0 * errors / total
    | where total >= ${var.api_5xx_min_requests} and error_pct > ${var.api_5xx_threshold_percent}
  EOT

  # SPEC-10 section 3: ingest freshness > 36 h on a weekday. Proxy: the last positive datapoint of the custom metric
  # tidal.ingest.rows_written (T-505) in AppMetrics, not the age of the latest bar per instrument (instrument ids are
  # deliberately not metric attributes, telemetry.py). The ingest job runs daily, so a missing datapoint means a missed
  # run. Weekday gate: UTC Monday to Friday. An empty window (no datapoint at all) counts as stale.
  kql_ingest_freshness = <<-EOT
    let weekday = toint(dayofweek(now()) / 1d);
    AppMetrics
    | where TimeGenerated > ago(2d) and Name == "tidal.ingest.rows_written" and Sum > 0
    | summarize last_write = max(TimeGenerated)
    | extend age_h = iff(isnull(last_write), 999, datetime_diff("hour", now(), last_write))
    | where weekday between (1 .. 5) and age_h > ${var.ingest_freshness_hours}
  EOT

  # SPEC-10 section 3: LA daily cap reached. The workspace writes "Data collection stopped" to its operations table when
  # the daily quota is hit (the operations table is exempt from the cap).
  kql_la_cap = <<-EOT
    _LogOperation
    | where TimeGenerated > ago(1h)
    | where Operation == "Data collection stopped" or Detail has "OverQuota"
  EOT

  # SPEC-10 section 3: spot eviction rate > 30 %/h. Proxy: spot nodes seen in the last hour that stopped reporting for
  # 10 minutes, minus nodes the cluster autoscaler removed (KubeEvents reason ScaleDown), divided by spot nodes seen.
  # Needs Container Insights. At least 3 spot nodes seen, so one lost node of two does not read as 50 %.
  kql_spot_eviction = <<-EOT
    let spot_nodes = KubeNodeInventory
        | where TimeGenerated > ago(1h)
        | where Labels contains 'scalesetpriority":"spot'
        | summarize last_seen = max(TimeGenerated) by Computer;
    let scaled_down = toscalar(KubeEvents
        | where TimeGenerated > ago(1h) and Reason == "ScaleDown"
        | summarize dcount(Name));
    spot_nodes
    | summarize nodes = count(), gone = countif(last_seen < ago(10m))
    | extend unplanned = max_of(gone - coalesce(scaled_down, 0), 0)
    | extend evicted_pct = 100.0 * unplanned / nodes
    | where nodes >= 3 and evicted_pct > ${var.spot_eviction_threshold_percent}
  EOT

  # Blob tag writes and deletes by callers outside the expected set (T-207 security note, ADR-0008 R-1 family).
  # Data-plane calls are not in the activity log, hence the StorageWrite/StorageDelete diagnostic categories.
  # Only attributable (OAuth) callers are listed: shared key and SAS are disabled on the account, and service-internal
  # operations such as lifecycle management carry no requester object id.
  kql_blob_audit = <<-EOT
    StorageBlobLogs
    | where TimeGenerated > ago(1h)
    | where OperationName in ("SetBlobTags", "DeleteBlob") and StatusText == "Success"
    | where isnotempty(RequesterObjectId) and RequesterObjectId !in (dynamic(${local.blob_expected_callers}))
  EOT
}

# --- Scheduled query alerts --------------------------------------------------------------------

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "api_5xx" {
  name                = "alert-tidal-api-5xx-${var.env}"
  display_name        = "Tidal ${var.env}: API 5xx above ${var.api_5xx_threshold_percent} % for 10 min"
  description         = "SPEC-10 section 3. More than ${var.api_5xx_threshold_percent} % of requests in the last 10 minutes returned 5xx (at least ${var.api_5xx_min_requests} requests)."
  resource_group_name = var.resource_group_name
  location            = var.location

  scopes               = [var.app_insights_id]
  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT10M"
  enabled              = true

  auto_mitigation_enabled = true

  criteria {
    query                   = local.kql_api_5xx
    time_aggregation_method = local.row_count_criteria.time_aggregation_method
    operator                = local.row_count_criteria.operator
    threshold               = local.row_count_criteria.threshold

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = local.action_group_ids
  }

  tags = local.tags
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "ingest_freshness" {
  name                = "alert-tidal-ingest-freshness-${var.env}"
  display_name        = "Tidal ${var.env}: no ingest for more than ${var.ingest_freshness_hours} h (weekdays)"
  description         = "SPEC-10 section 3. Proxy on the tidal.ingest.rows_written metric: no positive datapoint for ${var.ingest_freshness_hours} hours on a UTC weekday."
  resource_group_name = var.resource_group_name
  location            = var.location

  scopes               = [var.workspace_id]
  severity             = 2
  evaluation_frequency = "PT1H"
  window_duration      = "P2D"
  enabled              = true

  auto_mitigation_enabled = true

  criteria {
    query                   = local.kql_ingest_freshness
    time_aggregation_method = local.row_count_criteria.time_aggregation_method
    operator                = local.row_count_criteria.operator
    threshold               = local.row_count_criteria.threshold

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = local.action_group_ids
  }

  tags = local.tags
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "la_daily_cap" {
  name                = "alert-tidal-la-daily-cap-${var.env}"
  display_name        = "Tidal ${var.env}: Log Analytics daily cap reached"
  description         = "SPEC-10 section 3. The workspace stopped collecting data because the daily quota was reached; logs and traces are lost until the quota resets."
  resource_group_name = var.resource_group_name
  location            = var.location

  scopes               = [var.workspace_id]
  severity             = 1
  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  enabled              = true

  auto_mitigation_enabled = true

  criteria {
    query                   = local.kql_la_cap
    time_aggregation_method = local.row_count_criteria.time_aggregation_method
    operator                = local.row_count_criteria.operator
    threshold               = local.row_count_criteria.threshold

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = local.action_group_ids
  }

  tags = local.tags
}

# Needs the Container Insights tables, so it exists only when the cluster ships them (dev runs with Container Insights
# off for cost, so the rule is deferred there; see the module README).
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "spot_eviction" {
  count = var.container_insights_enabled ? 1 : 0

  name                = "alert-tidal-spot-eviction-${var.env}"
  display_name        = "Tidal ${var.env}: spot eviction rate above ${var.spot_eviction_threshold_percent} %/h"
  description         = "SPEC-10 section 3. More than ${var.spot_eviction_threshold_percent} % of the spot nodes seen in the last hour disappeared without an autoscaler scale-down (proxy for evictions)."
  resource_group_name = var.resource_group_name
  location            = var.location

  scopes               = [var.workspace_id]
  severity             = 3
  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  enabled              = true

  auto_mitigation_enabled = true

  criteria {
    query                   = local.kql_spot_eviction
    time_aggregation_method = local.row_count_criteria.time_aggregation_method
    operator                = local.row_count_criteria.operator
    threshold               = local.row_count_criteria.threshold

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = local.action_group_ids
  }

  tags = local.tags
}

# --- Blob audit: data-plane SetBlobTags / DeleteBlob by unexpected callers -----------------------

# Only the write and delete categories: StorageRead would log every API read (volume against the 0.2 GB/day cap) and
# reads are not a tampering signal. Metrics are not exported.
resource "azurerm_monitor_diagnostic_setting" "blob_audit" {
  count = var.enable_blob_audit ? 1 : 0

  name                       = "diag-tidal-blob-audit-${var.env}"
  target_resource_id         = "${var.storage_account_id}/blobServices/default"
  log_analytics_workspace_id = var.workspace_id

  enabled_log {
    category = "StorageWrite"
  }

  enabled_log {
    category = "StorageDelete"
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "blob_audit" {
  count = var.enable_blob_audit ? 1 : 0

  name                = "alert-tidal-blob-audit-${var.env}"
  display_name        = "Tidal ${var.env}: blob tags or deletes by an unexpected caller"
  description         = "SetBlobTags or DeleteBlob succeeded for a caller outside expected_blob_modifier_object_ids (T-207 security note). The activity log does not carry these data-plane calls, so this reads the blob diagnostic logs."
  resource_group_name = var.resource_group_name
  location            = var.location

  scopes               = [var.workspace_id]
  severity             = 1
  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  enabled              = true

  auto_mitigation_enabled = true

  criteria {
    query                   = local.kql_blob_audit
    time_aggregation_method = local.row_count_criteria.time_aggregation_method
    operator                = local.row_count_criteria.operator
    threshold               = local.row_count_criteria.threshold

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = local.action_group_ids
  }

  tags = local.tags

  depends_on = [azurerm_monitor_diagnostic_setting.blob_audit]
}

# --- Metric alert: dead-letter queue --------------------------------------------------------------

# One series per queue (dimension EntityName): the three queues stay inside the 10 free monitored time series.
resource "azurerm_monitor_metric_alert" "dlq" {
  name                = "alert-tidal-dlq-${var.env}"
  resource_group_name = var.resource_group_name
  description         = "SPEC-10 section 3 and messaging contract section 1. A dead-lettered message exists in any Service Bus entity: inspect with tidal dlq inspect."
  scopes              = [var.servicebus_namespace_id]
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT5M"
  enabled             = true
  auto_mitigate       = true

  criteria {
    metric_namespace = "Microsoft.ServiceBus/namespaces"
    metric_name      = "DeadletteredMessages"
    aggregation      = "Maximum"
    operator         = "GreaterThan"
    threshold        = 0

    dimension {
      name     = "EntityName"
      operator = "Include"
      values   = ["*"]
    }
  }

  action {
    action_group_id = var.action_group_id
  }

  tags = local.tags
}

# --- Activity-log alerts (free): control-plane changes that bypass the Terraform path (ADR-0008 R-1) ---------------

locals {
  activity_alerts = {
    mgmt-policy = {
      scope       = var.storage_account_id
      operation   = "Microsoft.Storage/storageAccounts/managementPolicies/write"
      description = "A storage lifecycle management policy was written (ADR-0008 R-1). A rewrite can demote or delete cold data. Expected only during a Terraform apply."
    }
    role-write = {
      scope       = var.resource_group_id
      operation   = "Microsoft.Authorization/roleAssignments/write"
      description = "A role assignment was created or changed in the resource group. docs/rbac/expected.json is the reference; expected only during a Terraform apply."
    }
    role-delete = {
      scope       = var.resource_group_id
      operation   = "Microsoft.Authorization/roleAssignments/delete"
      description = "A role assignment was deleted in the resource group. Expected only during a Terraform apply."
    }
  }
}

resource "azurerm_monitor_activity_log_alert" "control_plane" {
  for_each = local.activity_alerts

  name                = "alert-tidal-${each.key}-${var.env}"
  resource_group_name = var.resource_group_name
  location            = "global"
  description         = each.value.description
  scopes              = [each.value.scope]
  enabled             = true

  criteria {
    category       = "Administrative"
    operation_name = each.value.operation
  }

  action {
    action_group_id = var.action_group_id
  }

  tags = local.tags
}
