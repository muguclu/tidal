# Cost Management export + reader role (T-502, SPEC-09 section 2.1). Cost delta: docs/finops/deltas/T-502.md (0.00).

variable "ci_plan_principal_id" {
  description = "Object ID (not client ID) of the service principal of the read-only sp-tidal-ci-plan app. It receives Storage Blob Data Reader on the cost-exports container only, so the weekly cost workflow can read the export. null skips the assignment."
  type        = string
  default     = null

  validation {
    condition     = var.ci_plan_principal_id == null || can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", coalesce(var.ci_plan_principal_id, "x")))
    error_message = "ci_plan_principal_id must be a GUID or null."
  }
}

module "cost_export" {
  source = "../../modules/cost_export"

  env                   = local.env
  resource_group_id     = module.resource_group.id
  storage_account_id    = module.storage.id
  recurrence_start_date = var.budget_start_date
  reader_principal_id   = var.ci_plan_principal_id
  tags                  = module.resource_group.tags
  component             = "finops"
}

output "cost_export_container" {
  description = "Container holding the Cost Management export (cost.yml downloads from here)."
  value       = module.cost_export.container_name
}

output "cost_export_path_prefix" {
  description = "Blob prefix of the export runs inside the container."
  value       = module.cost_export.export_path_prefix
}
