variable "env" {
  description = "Environment name; part of the registry name (acr<project><env><suffix>)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the registry."
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

variable "name_suffix" {
  description = "Suffix that makes the registry name globally unique (acr<project><env><suffix>); supplied by the env composition. The full name (acrtidal + env + suffix) is alphanumeric and 5 to 50 characters by construction (3 to 8 character suffix) and is re-checked by a precondition."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.name_suffix))
    error_message = "name_suffix must be 3 to 8 lowercase letters or digits."
  }
}

variable "purge_schedule" {
  description = "Cron expression (UTC) for the weekly purge task; default is Sunday 03:00 UTC. Format: minute hour day-of-month month day-of-week."
  type        = string
  default     = "0 3 * * 0"

  validation {
    condition     = can(regex("^\\S+ \\S+ \\S+ \\S+ \\S+$", var.purge_schedule))
    error_message = "purge_schedule must be a five-field cron expression, e.g. \"0 3 * * 0\"."
  }
}

variable "purge_repository_regex" {
  description = "Anchored regular expression of repositories the purge task touches (the repository part of acr purge --filter '<repo regex>:<tag regex>'); keeps the task scoped to Tidal images. Must start with ^tidal and end with $, contain no single quote or whitespace."
  type        = string
  default     = "^tidal.*$"

  validation {
    condition     = can(regex("^\\^tidal[^'\\s]*[$]$", var.purge_repository_regex))
    error_message = "purge_repository_regex must start with ^tidal, end with $, and contain no single quote or whitespace (so it can never match all repositories)."
  }
}

variable "untagged_retention_days" {
  description = "Untagged manifests older than this many days are deleted by the weekly purge (acr purge --untagged --ago <n>d)."
  type        = number
  default     = 7

  validation {
    condition     = var.untagged_retention_days >= 1 && floor(var.untagged_retention_days) == var.untagged_retention_days
    error_message = "untagged_retention_days must be a whole number of at least 1."
  }
}

variable "sha_tag_retention_days" {
  description = "Tags matching sha-* (CI commit tags) older than this many days are deleted by the weekly purge. v* release tags and other tags are never purged."
  type        = number
  default     = 30

  validation {
    condition     = var.sha_tag_retention_days >= 1 && floor(var.sha_tag_retention_days) == var.sha_tag_retention_days
    error_message = "sha_tag_retention_days must be a whole number of at least 1."
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
  description = "Value of the component tag for the resources in this module (for example registry); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
