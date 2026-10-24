# Workbook and alerts (T-506, SPEC-10 sections 2 and 3). Cost delta: docs/finops/deltas/T-506.md.
# The email action group is the one modules/budget already creates; no second group exists.

module "monitoring" {
  source = "../../modules/monitoring"

  env                 = local.env
  resource_group_name = module.resource_group.name
  resource_group_id   = module.resource_group.id
  location            = module.resource_group.location
  tags                = module.resource_group.tags
  component           = "observability"

  action_group_id         = module.budget.action_group_id
  app_insights_id         = module.log_analytics.app_insights_id
  workspace_id            = module.log_analytics.workspace_id
  servicebus_namespace_id = module.servicebus.id
  storage_account_id      = module.storage.id
  sql_database_id         = module.sql.database_id

  # Mirrors enable_container_insights in aks.tf (off: ~EUR 10 to 20/month of extra ingestion against the 0.2 GB/day cap).
  # Turning it on there and here creates the spot eviction alert and fills the KEDA/node tiles.
  container_insights_enabled = false

  # Only these two identities may set blob tags or delete blobs (tiering compacts and retags, ingest rewrites bars).
  expected_blob_modifier_object_ids = [
    module.identity.identities["id-tiering"].principal_id,
    module.identity.identities["id-ingest"].principal_id,
  ]
}

output "workbook_id" {
  description = "Resource ID of the Tidal Azure Workbook (Azure portal: Monitor > Workbooks)."
  value       = module.monitoring.workbook_id
}

output "alert_rule_ids" {
  description = "Alert name to resource ID for every T-506 alert."
  value       = module.monitoring.alert_rule_ids
}
