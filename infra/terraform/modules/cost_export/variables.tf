variable "env" {
  description = "Environment name; used in the export name (export-tidal-<env>-actual)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_id" {
  description = "Resource ID of the resource group whose costs are exported: /subscriptions/<guid>/resourceGroups/<name>."
  type        = string

  validation {
    condition     = can(regex("(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+$", var.resource_group_id))
    error_message = "resource_group_id must be a resource group ID: /subscriptions/<guid>/resourceGroups/<name>."
  }
}

variable "storage_account_id" {
  description = "Resource ID of the storage account that receives the export (the environment account, module.storage.id)."
  type        = string

  validation {
    condition     = can(regex("(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft.Storage/storageAccounts/[a-z0-9]{3,24}$", var.storage_account_id))
    error_message = "storage_account_id must be a storage account resource ID."
  }
}

variable "container_name" {
  description = "Container created for the export."
  type        = string
  default     = "cost-exports"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9]|-[a-z0-9]){2,62}$", var.container_name))
    error_message = "container_name must be 3-63 characters: lowercase letters, digits and single hyphens."
  }
}

variable "root_folder_path" {
  description = "Folder inside the container. Cost Management writes <root>/<export name>/<YYYYMMDD-YYYYMMDD>/<run id>/part_*.csv."
  type        = string
  default     = "tidal"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", var.root_folder_path))
    error_message = "root_folder_path must be a relative path of letters, digits, dot, underscore and hyphen segments."
  }
}

variable "recurrence_start_date" {
  description = "Start of the daily schedule, RFC 3339 UTC (for example 2026-11-01T00:00:00Z). Azure may reject a start date in the past on create; pass today or later if so. Later changes are ignored (lifecycle)."
  type        = string

  validation {
    condition     = can(formatdate("YYYY", var.recurrence_start_date))
    error_message = "recurrence_start_date must be an RFC 3339 timestamp."
  }
}

variable "recurrence_end_date" {
  description = "Optional end of the schedule (RFC 3339). null means ten years after the start."
  type        = string
  default     = null

  validation {
    condition     = var.recurrence_end_date == null || can(formatdate("YYYY", var.recurrence_end_date))
    error_message = "recurrence_end_date must be null or an RFC 3339 timestamp."
  }
}

variable "reader_principal_id" {
  description = "Object ID of the service principal that may read the export (sp-tidal-ci-plan). It receives Storage Blob Data Reader on the export container only. null skips the assignment."
  type        = string
  default     = null

  validation {
    condition     = var.reader_principal_id == null || can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", coalesce(var.reader_principal_id, "x")))
    error_message = "reader_principal_id must be a GUID or null."
  }
}

variable "tags" {
  description = "Tag map from module.resource_group.tags (project, env, owner, cost_center, component, park). The export resource has no tags argument; the map is kept for the module interface and validated."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["project", "env", "owner", "cost_center", "component", "park"] :
      contains(keys(var.tags), k) && length(trimspace(var.tags[k])) > 0
    ])
    error_message = "tags must contain non-empty project, env, owner, cost_center, component, park."
  }
}

variable "component" {
  description = "Value of the component tag (finops); merged over tags[\"component\"]."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
