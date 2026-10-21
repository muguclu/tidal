variable "subscription_id" {
  description = "Azure subscription ID to deploy into (explicit; never inferred from the az CLI context)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a GUID."
  }
}

variable "state_account_name" {
  description = "Globally unique name of the Terraform state storage account (3-24 chars, lowercase letters and digits), e.g. sttidaltfstate<suffix>."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.state_account_name))
    error_message = "state_account_name must be 3-24 characters, lowercase letters and digits only."
  }
}

variable "location" {
  description = "Azure region for the state resource group and account."
  type        = string
  default     = "westeurope"

  validation {
    condition     = can(regex("^[a-z0-9]+$", var.location))
    error_message = "location must be a lowercase Azure region name without spaces, e.g. westeurope."
  }
}

variable "env" {
  description = "Environment tag value, state blob key prefix and the GitHub environment name bound to the main OIDC credential."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "owner" {
  description = "Value of the owner tag (person or team accountable for the resources)."
  type        = string

  validation {
    condition     = length(trimspace(var.owner)) > 0
    error_message = "owner must not be empty."
  }
}

variable "cost_center" {
  description = "Value of the cost_center tag."
  type        = string
  default     = "portfolio"

  validation {
    condition     = length(trimspace(var.cost_center)) > 0
    error_message = "cost_center must not be empty."
  }
}

variable "park" {
  description = "Value of the park tag. The state account must survive parking, so this is normally false (bootstrap is never touched by make park)."
  type        = bool
  default     = false
}

variable "github_owner" {
  description = "GitHub user or organisation that owns the repository trusted by the CI identity."
  type        = string
  default     = "muguclu"

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+$", var.github_owner))
    error_message = "github_owner may only contain letters, digits and hyphens."
  }
}

variable "github_repo" {
  description = "GitHub repository name trusted by the CI identity."
  type        = string
  default     = "tidal"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo may only contain letters, digits, '.', '_' and '-'."
  }
}

variable "state_soft_delete_days" {
  description = "Soft-delete retention (days) for blobs and containers in the state account."
  type        = number
  default     = 7

  validation {
    condition     = var.state_soft_delete_days >= 1 && var.state_soft_delete_days <= 365
    error_message = "state_soft_delete_days must be between 1 and 365."
  }
}
