variable "name" {
  description = "Storage account name: globally unique, 3-24 lowercase letters and digits (for example sttidaldev<suffix>)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.name))
    error_message = "name must be 3-24 characters, lowercase letters and digits only."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the storage account."
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

variable "enable_private_networking" {
  description = "Private networking flag (SPEC-06 section 4). false (default): the public endpoint stays enabled and access is controlled by Entra ID + RBAC only. true: public network access is disabled and a private endpoint (network module, Phase 4) is required."
  type        = bool
  default     = false
}

variable "containers" {
  description = "Blob containers to create (storage-layout contract section 1; marketdata per ADR-0010). Flat namespace; lifecycle rules are attached by T-207."
  type        = set(string)
  default     = ["raw", "marketdata", "reference", "backtests", "exports", "bench"]

  validation {
    condition     = alltrue([for c in var.containers : can(regex("^[a-z0-9]([a-z0-9]|-[a-z0-9]){2,62}$", c))])
    error_message = "Container names must be 3-63 characters: lowercase letters, digits and single hyphens, starting and ending with a letter or digit."
  }
}

variable "soft_delete_days" {
  description = "Retention in days for blob and container soft delete (SPEC-06 section 3: 7)."
  type        = number
  default     = 7

  validation {
    condition     = var.soft_delete_days >= 1 && var.soft_delete_days <= 365
    error_message = "soft_delete_days must be between 1 and 365."
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
