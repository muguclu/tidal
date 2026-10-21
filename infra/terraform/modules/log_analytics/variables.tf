variable "env" {
  description = "Environment name; used in resource names (log-tidal-<env>, appi-tidal-<env>)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the workspace and Application Insights."
  type        = string

  validation {
    condition     = length(var.resource_group_name) > 0
    error_message = "resource_group_name must not be empty."
  }
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "westeurope"

  validation {
    condition     = can(regex("^[a-z0-9]+$", var.location))
    error_message = "location must be a lowercase Azure region name without spaces, e.g. westeurope."
  }
}

variable "daily_quota_gb" {
  description = "Workspace daily ingestion cap in GB (SPEC-07 section 2: 0.2). This is the cost ceiling; data above the cap is dropped until the daily reset."
  type        = number
  default     = 0.2

  validation {
    condition     = var.daily_quota_gb > 0
    error_message = "daily_quota_gb must be greater than 0 (-1, unlimited, is deliberately not allowed)."
  }
}

variable "retention_in_days" {
  description = "Workspace and Application Insights retention in days (SPEC-07: 30). Restricted to the values Application Insights accepts."
  type        = number
  default     = 30

  validation {
    condition     = contains([30, 60, 90, 120, 180, 270, 365, 550, 730], var.retention_in_days)
    error_message = "retention_in_days must be one of 30, 60, 90, 120, 180, 270, 365, 550, 730 (Application Insights allowed values)."
  }
}

variable "sampling_percentage" {
  description = "Application Insights ingestion sampling percentage applied service-side (100 = keep all). Keep at 100: trace sampling (20 percent, 100 percent on errors) is SDK-side per SPEC-10 section 1; do not lower this to reach it."
  type        = number
  default     = 100

  validation {
    condition     = var.sampling_percentage > 0 && var.sampling_percentage <= 100
    error_message = "sampling_percentage must be in (0, 100]."
  }
}

variable "tags" {
  description = "Tag map from module.resource_group.tags. Must contain project, env, owner, cost_center, component, park (non-empty), park must be true or false, project must be tidal. The component tag is overridden per resource from var.component."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["project", "env", "owner", "cost_center", "component", "park"] :
      contains(keys(var.tags), k) && length(trimspace(var.tags[k])) > 0
      ]) && (
      !contains(keys(var.tags), "park") ? false : contains(["true", "false"], var.tags["park"])
      ) && (
      !contains(keys(var.tags), "project") ? false : var.tags["project"] == "tidal"
    )
    error_message = "tags must contain non-empty project, env, owner, cost_center, component, park; park must be \"true\" or \"false\"; project must be \"tidal\"."
  }
}

variable "component" {
  description = "Value of the component tag for the resources in this module (for example observability); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
