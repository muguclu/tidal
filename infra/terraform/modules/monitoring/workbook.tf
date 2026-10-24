# Azure Workbook with the four SPEC-10 section 2 sections. Everything is KQL over Application Insights (classic table
# names, resource scope) or the workspace, plus Azure Monitor metric tiles. Panels that cannot be built without live data
# are labelled PLACEHOLDER in their title. The JSON is produced from the lists below so each panel is one reviewable line.

locals {
  ai = {
    type = "microsoft.insights/components"
    ids  = [var.app_insights_id]
  }
  la = {
    type = "microsoft.operationalinsights/workspaces"
    ids  = [var.workspace_id]
  }

  # --- Section 1: platform health -------------------------------------------------------------
  # --- Section 2: batch -----------------------------------------------------------------------
  # --- Section 3: data ------------------------------------------------------------------------
  # --- Section 4: cost ------------------------------------------------------------------------
  kql_panels = [
    {
      key   = "api-latency"
      title = "API latency p50 / p95 / p99 (ms)"
      src   = "ai"
      viz   = "timechart"
      query = "requests | summarize p50 = percentile(duration, 50), p95 = percentile(duration, 95), p99 = percentile(duration, 99) by bin(timestamp, 15m) | order by timestamp asc"
    },
    {
      key   = "api-errors"
      title = "API error rate (% of requests with status 5xx)"
      src   = "ai"
      viz   = "timechart"
      query = "requests | summarize error_pct = 100.0 * countif(toint(resultCode) >= 500) / count() by bin(timestamp, 15m) | order by timestamp asc"
    },
    {
      key   = "dependency-failures"
      title = "Dependency failures by target"
      src   = "ai"
      viz   = "barchart"
      query = "dependencies | where success == false | summarize failures = count() by target, type | order by failures desc | take 15"
    },
    {
      key   = "queue-lag"
      title = "Worker queue lag (tidal.worker.queue_lag_ms, p95 by queue)"
      src   = "ai"
      viz   = "timechart"
      query = "customMetrics | where name == 'tidal.worker.queue_lag_ms' | extend queue = tostring(customDimensions.queue) | summarize p95_ms = percentile(valueMax, 95) by queue, bin(timestamp, 15m)"
    },
    {
      key   = "backtest-duration"
      title = "Backtest duration (tidal.backtest.duration_ms, avg and max by outcome)"
      src   = "ai"
      viz   = "timechart"
      query = "customMetrics | where name == 'tidal.backtest.duration_ms' | extend outcome = tostring(customDimensions.outcome) | summarize avg_ms = sum(valueSum) / sum(valueCount), max_ms = max(valueMax) by outcome, bin(timestamp, 1h)"
    },
    {
      key   = "cold-start"
      title = "Cold start (tidal.process.cold_start_ms, max by service)"
      src   = "ai"
      viz   = "timechart"
      query = "customMetrics | where name == 'tidal.process.cold_start_ms' | extend service = tostring(customDimensions.service) | summarize max_ms = max(valueMax) by service, bin(timestamp, 1h)"
    },
    {
      key   = "keda-replicas"
      title = "KEDA worker replicas (pods per 5 min; needs Container Insights, empty while it is off)"
      src   = "la"
      viz   = "timechart"
      query = "KubePodInventory | where Namespace == 'tidal' and PodStatus == 'Running' | summarize replicas = dcount(Name) by bin(TimeGenerated, 5m)"
    },
    {
      key   = "node-count"
      title = "Nodes per pool (needs Container Insights, empty while it is off)"
      src   = "la"
      viz   = "timechart"
      query = "KubeNodeInventory | extend pool = tostring(parse_json(Labels)[0].agentpool) | summarize nodes = dcount(Computer) by pool, bin(TimeGenerated, 15m)"
    },
    {
      key   = "spot-evictions"
      title = "Spot nodes that stopped reporting (eviction proxy; same signal as the alert; needs Container Insights)"
      src   = "la"
      viz   = "table"
      query = "KubeNodeInventory | where TimeGenerated > ago(24h) and Labels contains 'scalesetpriority\":\"spot' | summarize first_seen = min(TimeGenerated), last_seen = max(TimeGenerated) by Computer | where last_seen < ago(10m) | order by last_seen desc"
    },
    {
      key   = "rows-per-tier"
      title = "Rows served per tier (tidal.query.rows)"
      src   = "ai"
      viz   = "barchart"
      query = "customMetrics | where name == 'tidal.query.rows' | extend tier = tostring(customDimensions.tier) | summarize rows = sum(valueSum) by tier, bin(timestamp, 1d)"
    },
    {
      key   = "bytes-scanned"
      title = "Bytes scanned (tidal.query.bytes_scanned) by tier"
      src   = "ai"
      viz   = "timechart"
      query = "customMetrics | where name == 'tidal.query.bytes_scanned' | extend tier = tostring(customDimensions.tier) | summarize bytes = sum(valueSum) by tier, bin(timestamp, 1h)"
    },
    {
      key   = "rows-moved"
      title = "Rows moved by tiering (tidal.tiering.rows_moved)"
      src   = "ai"
      viz   = "timechart"
      query = "customMetrics | where name == 'tidal.tiering.rows_moved' | extend direction = tostring(customDimensions.direction) | summarize rows = sum(valueSum) by direction, bin(timestamp, 1d)"
    },
    {
      key   = "ingest-freshness"
      title = "Ingest freshness: hours since the last positive tidal.ingest.rows_written per source (per source, not per instrument)"
      src   = "ai"
      viz   = "table"
      query = "customMetrics | where timestamp > ago(7d) and name == 'tidal.ingest.rows_written' and valueSum > 0 | extend source = tostring(customDimensions.source) | summarize last_write = max(timestamp) by source | extend age_hours = datetime_diff('hour', now(), last_write) | order by age_hours desc"
    },
    {
      key   = "queries-per-day"
      title = "Cost denominator: queries per day (tidal.query.rows datapoints) - divide the daily cost by this / 1000"
      src   = "ai"
      viz   = "barchart"
      query = "customMetrics | where name == 'tidal.query.rows' | summarize queries = sum(valueCount) by bin(timestamp, 1d)"
    },
    {
      key   = "backtests-per-day"
      title = "Cost denominator: backtests per day (tidal.backtest.duration_ms datapoints)"
      src   = "ai"
      viz   = "barchart"
      query = "customMetrics | where name == 'tidal.backtest.duration_ms' | summarize backtests = sum(valueCount) by bin(timestamp, 1d)"
    },
    {
      key   = "cost-by-component"
      title = "PLACEHOLDER: daily cost by component tag (needs the T-502 Cost Management export in a queryable store; the export is CSV in Blob and is read by the weekly report, not by this workbook)"
      src   = "la"
      viz   = "table"
      query = "datatable(day: datetime, component: string, cost_eur: real, note: string) [datetime(null), 'PLACEHOLDER', real(null), 'Wire the cost export (T-502) into Log Analytics or ADX, then replace this query. See docs/finops/results/ for the weekly report.']"
    },
    {
      key   = "cost-per-unit"
      title = "PLACEHOLDER: EUR per backtest and EUR per 1k queries (daily cost / the two denominators above)"
      src   = "la"
      viz   = "table"
      query = "datatable(metric: string, eur: real, note: string) ['eur_per_backtest', real(null), 'PLACEHOLDER until the cost export is queryable', 'eur_per_1k_queries', real(null), 'PLACEHOLDER until the cost export is queryable']"
    },
  ]

  # Metric tiles: Azure Monitor platform metrics. metric ids are <namespace>--<name>; aggregation 1 = average, 3 = max, 4 = total.
  metric_panels = concat(
    [
      {
        key       = "sb-active"
        title     = "Service Bus active messages (queue depth) by queue"
        resource  = var.servicebus_namespace_id
        namespace = "microsoft.servicebus/namespaces"
        metric    = "ActiveMessages"
        agg       = 3
        split     = "EntityName"
      },
      {
        key       = "sb-dlq"
        title     = "Service Bus dead-lettered messages by queue"
        resource  = var.servicebus_namespace_id
        namespace = "microsoft.servicebus/namespaces"
        metric    = "DeadletteredMessages"
        agg       = 3
        split     = "EntityName"
      },
      {
        key       = "blob-capacity"
        title     = "Blob capacity by tier"
        resource  = "${var.storage_account_id}/blobServices/default"
        namespace = "microsoft.storage/storageaccounts/blobservices"
        metric    = "BlobCapacity"
        agg       = 1
        split     = "Tier"
      },
    ],
    var.sql_database_id == null ? [] : [
      {
        key       = "sql-space"
        title     = "SQL space used (bytes)"
        resource  = var.sql_database_id
        namespace = "microsoft.sql/servers/databases"
        metric    = "storage"
        agg       = 3
        split     = null
      },
    ],
  )

  wb_time = { durationMs = 86400000 }

  wb_header = {
    "1-platform" = "## 1. Platform health\nAPI latency and errors, dependency failures, Service Bus queue depth and dead letters."
    "2-batch"    = "## 2. Batch\nKEDA replicas against queue depth, nodes per pool, spot evictions. The pod, node and eviction tiles read Container Insights and stay empty while it is off (dev default)."
    "3-data"     = "## 3. Data\nRows per tier, SQL space, Blob capacity per tier, ingest freshness."
    "4-cost"     = "## 4. Cost\nCost tiles marked PLACEHOLDER need the T-502 export. Query and backtest volumes are real."
  }

  wb_text = { for k, v in local.wb_header : k => { type = 1, name = "text-${k}", content = { json = v } } }

  wb_kql = { for p in local.kql_panels : p.key => {
    type = 3
    name = "kql-${p.key}"
    content = {
      version                 = "KqlItem/1.0"
      query                   = p.query
      size                    = 1
      title                   = p.title
      timeContext             = local.wb_time
      queryType               = 0
      resourceType            = local.wb[p.src].type
      crossComponentResources = local.wb[p.src].ids
      visualization           = p.viz
    }
  } }

  wb = { ai = local.ai, la = local.la }

  wb_metrics = { for p in local.metric_panels : p.key => {
    type = 10
    name = "metric-${p.key}"
    content = {
      version      = "MetricsItem/2.0"
      size         = 1
      chartType    = 2
      title        = p.title
      resourceType = p.namespace
      metricScope  = 0
      resourceIds  = [p.resource]
      timeContext  = local.wb_time
      metrics = [{
        namespace   = p.namespace
        metric      = "${p.namespace}--${p.metric}"
        aggregation = p.agg
      }]
      filters = p.split == null ? [] : [{ id = "split", dimension = p.split, operator = 0, values = [] }]
    }
  } }

  # Order of tiles per section. Missing optional tiles (no SQL database) are dropped.
  wb_layout = [
    "text:1-platform", "kql:api-latency", "kql:api-errors", "kql:dependency-failures", "metric:sb-active", "metric:sb-dlq", "kql:queue-lag", "kql:cold-start",
    "text:2-batch", "kql:keda-replicas", "kql:node-count", "kql:spot-evictions", "kql:backtest-duration",
    "text:3-data", "kql:rows-per-tier", "kql:bytes-scanned", "kql:rows-moved", "metric:blob-capacity", "metric:sql-space", "kql:ingest-freshness",
    "text:4-cost", "kql:queries-per-day", "kql:backtests-per-day", "kql:cost-by-component", "kql:cost-per-unit",
  ]

  wb_items = [
    for ref in local.wb_layout :
    merge(local.wb_text, local.wb_kql, local.wb_metrics)[split(":", ref)[1]]
    if contains(keys(merge(local.wb_text, local.wb_kql, local.wb_metrics)), split(":", ref)[1])
  ]

  workbook_json = jsonencode({
    version             = "Notebook/1.0"
    items               = local.wb_items
    fallbackResourceIds = [var.app_insights_id]
    "$schema"           = "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json"
  })
}

resource "azurerm_application_insights_workbook" "tidal" {
  # The workbook name must be a GUID. A name-based UUID keeps it stable across applies and environments.
  name                = uuidv5("dns", "workbook.tidal.${var.env}")
  resource_group_name = var.resource_group_name
  location            = var.location
  display_name        = "Tidal ${var.env}: platform, batch, data, cost"
  source_id           = lower(var.app_insights_id)
  category            = "workbook"
  data_json           = local.workbook_json

  tags = local.tags
}
