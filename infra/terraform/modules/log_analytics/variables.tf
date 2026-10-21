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
  description = "Workspace and Application Insights retention in days (SPEC-07: 30, the free-retention tier)."
  type        = number
  default     = 30

  validation {
    condition     = var.retention_in_days >= 30 && var.retention_in_days <= 730
    error_message = "retention_in_days must be between 30 and 730."
  }
}

variable "sampling_percentage" {
  description = "Application Insights ingestion sampling percentage applied service-side (100 = keep all). Lower it, or enable SDK-side sampling in OpenTelemetry (parentbased_traceidratio), to cut volume before the daily cap bites."
  type        = number
  default     = 100

  validation {
    condition     = var.sampling_percentage > 0 && var.sampling_percentage <= 100
    error_message = "sampling_percentage must be in (0, 100]."
  }
}

variable "tags" {
  description = "Complete tag map for this resource. Must contain the mandatory keys project, env, owner, cost_center, component, park (pass module.resource_group.tags and override component)."
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["project", "env", "owner", "cost_center", "component", "park"] : contains(keys(var.tags), k) && length(trimspace(var.tags[k])) > 0])
    error_message = "tags must contain non-empty values for: project, env, owner, cost_center, component, park."
  }
}
