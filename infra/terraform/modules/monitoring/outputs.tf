output "workbook_id" {
  description = "Resource ID of the Tidal Azure Workbook."
  value       = azurerm_application_insights_workbook.tidal.id
}

output "alert_rule_ids" {
  description = "Map of alert name to resource ID for every alert this module created (the spot eviction alert is absent while container_insights_enabled is false)."
  value = merge(
    {
      api-5xx          = azurerm_monitor_scheduled_query_rules_alert_v2.api_5xx.id
      ingest-freshness = azurerm_monitor_scheduled_query_rules_alert_v2.ingest_freshness.id
      la-daily-cap     = azurerm_monitor_scheduled_query_rules_alert_v2.la_daily_cap.id
      dlq              = azurerm_monitor_metric_alert.dlq.id
    },
    { for k, a in azurerm_monitor_activity_log_alert.control_plane : k => a.id },
    { for r in azurerm_monitor_scheduled_query_rules_alert_v2.spot_eviction : "spot-eviction" => r.id },
    { for r in azurerm_monitor_scheduled_query_rules_alert_v2.blob_audit : "blob-audit" => r.id },
  )
}
