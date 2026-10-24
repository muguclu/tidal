variable "subscription_id" {
  description = "Azure subscription GUID that hosts the dev environment."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a GUID."
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

variable "location" {
  description = "Azure region for every resource (SPEC-07: westeurope)."
  type        = string
  default     = "westeurope"

  validation {
    condition     = can(regex("^[a-z0-9]+$", var.location))
    error_message = "location must be a lowercase Azure region name without spaces, e.g. westeurope."
  }
}

variable "name_suffix" {
  description = "Single suffix that makes globally unique names (storage, Key Vault, ACR, SQL server, Service Bus namespace) unique. 3 to 8 lowercase letters or digits."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.name_suffix))
    error_message = "name_suffix must be 3 to 8 lowercase letters or digits."
  }
}

variable "park" {
  description = "Value of the park tag: true marks the environment as parked (make park / make unpark flip it)."
  type        = bool
  default     = false
}

variable "enable_private_networking" {
  description = "Private networking (SPEC-06 section 4, T-404): VNet, private endpoints for Blob, SQL and Key Vault, closed public access on those three, internal tidal-query, App Service VNet integration, snet-vm with explicit outbound. false (default) creates none of it and costs nothing. Changing it on a live environment replaces the AKS node pools and the Container Apps environment, and Terraform itself then needs a network path to Blob and Key Vault (see the module README)."
  type        = bool
  default     = false
}

variable "enable_ingest_topic" {
  description = "Create the optional ingest-events topic. Switches Service Bus from Basic to Standard (adds a base charge, see docs/finops/deltas/T-124.md)."
  type        = bool
  default     = false
}

variable "use_free_offer" {
  description = "Create the SQL database on the Azure SQL free offer (ADR-0011). Dev-only cost lever."
  type        = bool
  default     = true
}

variable "sql_entra_admin_login" {
  description = "Display name of the Entra group that administers the SQL server (for example sg-tidal-sql-admins). Create the group first, see README."
  type        = string

  validation {
    condition     = length(trimspace(var.sql_entra_admin_login)) > 0
    error_message = "sql_entra_admin_login must not be empty."
  }
}

variable "sql_entra_admin_object_id" {
  description = "Object ID (GUID) of the Entra group that administers the SQL server."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.sql_entra_admin_object_id))
    error_message = "sql_entra_admin_object_id must be a GUID."
  }
}

variable "alert_emails" {
  description = "1 to 10 email addresses that receive budget alerts through the action group."
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) >= 1 && length(var.alert_emails) <= 10
    error_message = "alert_emails must contain 1 to 10 addresses."
  }
}

variable "budget_start_date" {
  description = "First day of the current month, midnight UTC, RFC 3339 (for example 2026-11-01T00:00:00Z). Azure rejects earlier dates for new budgets."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-(0[1-9]|1[0-2])-01T00:00:00Z$", var.budget_start_date))
    error_message = "budget_start_date must be the first of a month at 00:00:00Z, e.g. 2026-11-01T00:00:00Z."
  }
}

variable "budget_scope" {
  description = "subscription (covers the AKS MC_* node group, default) or resource_group."
  type        = string
  default     = "subscription"

  validation {
    condition     = contains(["subscription", "resource_group"], var.budget_scope)
    error_message = "budget_scope must be subscription or resource_group."
  }
}
