variable "env" {
  description = "Environment name; used in the server name (sql-tidal-<env>)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the SQL server and database."
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

variable "server_name_suffix" {
  description = "Suffix that makes the server name globally unique (sql-tidal-<env>-<suffix>); supplied by the env composition."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.server_name_suffix))
    error_message = "server_name_suffix must be 3 to 8 lowercase letters or digits."
  }
}

variable "tenant_id" {
  description = "Entra tenant ID of the administrator."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.tenant_id))
    error_message = "tenant_id must be a GUID."
  }
}

variable "entra_admin_login" {
  description = "Display name of the Entra administrator. An Entra group is recommended (membership changes need no Terraform run); a single user works too. Never a SQL login."
  type        = string

  validation {
    condition     = length(trimspace(var.entra_admin_login)) > 0
    error_message = "entra_admin_login must not be empty."
  }
}

variable "entra_admin_object_id" {
  description = "Object ID of the Entra administrator (group recommended)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.entra_admin_object_id))
    error_message = "entra_admin_object_id must be a GUID."
  }
}

variable "enable_system_assigned_identity" {
  description = "Give the server a system-assigned identity (needed later for TDE with a customer-managed key or auditing to storage)."
  type        = bool
  default     = false
}

variable "enable_private_networking" {
  description = "Phase 4 private networking flag (SPEC-06 section 4). When true the public endpoint is disabled and no firewall rule is created; the private endpoint itself is owned by the network module."
  type        = bool
  default     = false
}

variable "allow_azure_services" {
  description = "Create the 0.0.0.0 Allow Azure services firewall rule. It allows Azure IPs of any tenant to reach the server on TDS (Entra-only authentication still applies), so it is off by default. Prefer specific IP rules until T-404 (private networking). Ignored when enable_private_networking is true. A 0.0.0.0/0 public rule is never created."
  type        = bool
  default     = false
}

variable "allowed_ip_addresses" {
  description = "Public IPv4 addresses allowed through the SQL firewall, one single-address rule each (start = end). Typically the outbound IPs of App Service and Container Apps plus an operator IP while enable_private_networking is false. No ranges, no 0.0.0.0. Ignored (no rules) when enable_private_networking is true. Must be known at plan time because the addresses become resource keys."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for ip in var.allowed_ip_addresses :
      can(regex("^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$", ip)) && !contains(["0.0.0.0", "255.255.255.255"], ip)
    ])
    error_message = "allowed_ip_addresses must contain single dotted-quad IPv4 addresses (no CIDR ranges) and must not contain 0.0.0.0 or 255.255.255.255."
  }
}

variable "database_name" {
  description = "Database name (config-contract TIDAL_SQL__DATABASE default: tidal)."
  type        = string
  default     = "tidal"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,127}$", var.database_name))
    error_message = "database_name must start with a letter and contain only letters, digits and underscores (max 128)."
  }
}

variable "sku_name" {
  description = "Database SKU. SPEC-07 section 2: GP serverless Gen5 with max 2 vCore, i.e. GP_S_Gen5_2. Only serverless GP Gen5 names are accepted."
  type        = string
  default     = "GP_S_Gen5_2"

  validation {
    condition     = can(regex("^GP_S_Gen5_(1|2|4)$", var.sku_name))
    error_message = "sku_name must be a serverless General Purpose Gen5 SKU: GP_S_Gen5_1, GP_S_Gen5_2 or GP_S_Gen5_4."
  }
}

variable "min_capacity" {
  description = "Minimum vCores while active (SPEC-07: 0.5). Must not exceed the SKU vCore count."
  type        = number
  default     = 0.5

  validation {
    condition     = contains([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 3, 4], var.min_capacity)
    error_message = "min_capacity must be a valid serverless minimum (0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 3, 4)."
  }
}

variable "auto_pause_delay_in_minutes" {
  description = "Idle minutes before auto-pause (SPEC-07: 60). -1 disables pausing and is deliberately not allowed; the minimum accepted by Azure is 15, SPEC-07 sets 60."
  type        = number
  default     = 60

  validation {
    condition     = var.auto_pause_delay_in_minutes >= 15 && var.auto_pause_delay_in_minutes <= 10080
    error_message = "auto_pause_delay_in_minutes must be between 15 and 10080 (auto-pause may not be disabled)."
  }
}

variable "max_size_gb" {
  description = "Maximum database size in GB (SPEC-07: 32, which is also the free offer storage limit)."
  type        = number
  default     = 32

  validation {
    condition     = var.max_size_gb >= 1 && var.max_size_gb <= 1024 && floor(var.max_size_gb) == var.max_size_gb
    error_message = "max_size_gb must be a whole number between 1 and 1024."
  }
}

variable "use_free_offer" {
  description = "Apply the Azure SQL free offer (100,000 vCore-seconds and 32 GB per month, AutoPause at the limit) through azapi (ADR-0011). Dev-only cost lever; set false for prod or when the subscription already has 10 free databases."
  type        = bool
  default     = true
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
  description = "Value of the component tag for the resources in this module (for example data); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}

variable "log_analytics_workspace_id" {
  description = "Resource ID of the Log Analytics workspace that receives the SQL audit events (module log_analytics output workspace_id). null disables server auditing (only for isolated module tests)."
  type        = string
  default     = null

  validation {
    condition     = var.log_analytics_workspace_id == null || can(regex("(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.OperationalInsights/workspaces/[^/]+$", var.log_analytics_workspace_id))
    error_message = "log_analytics_workspace_id must be null or a Log Analytics workspace resource ID."
  }
}

variable "enable_defender_for_sql" {
  description = "Enable Microsoft Defender for SQL on this server (13.20 EUR/month per server, billed while the server exists, not reduced by auto-pause). Off by default per ADR-0017."
  type        = bool
  default     = false
}

variable "enable_vulnerability_assessment" {
  description = "Enable SQL Vulnerability Assessment (Express configuration, no storage account). Only effective when enable_defender_for_sql is true. Off by default per ADR-0017."
  type        = bool
  default     = false
}

variable "audit_action_groups" {
  description = "Server audit action groups. Dev default is low volume: failed logins and security-relevant changes. Add SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP or BATCH_COMPLETED_GROUP only with a daily-cap check (docs/finops/deltas/T-407_model.py), because the workspace cap is 0.2 GB/day."
  type        = list(string)
  default = [
    "FAILED_DATABASE_AUTHENTICATION_GROUP",
    "DATABASE_PRINCIPAL_CHANGE_GROUP",
    "DATABASE_ROLE_MEMBER_CHANGE_GROUP",
    "DATABASE_PERMISSION_CHANGE_GROUP",
    "SCHEMA_OBJECT_CHANGE_GROUP",
  ]

  validation {
    condition     = length(var.audit_action_groups) > 0 && alltrue([for g in var.audit_action_groups : can(regex("^[A-Z_]+_GROUP$", g))])
    error_message = "audit_action_groups must be a non-empty list of audit action group names such as FAILED_DATABASE_AUTHENTICATION_GROUP."
  }
}
