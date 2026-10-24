variable "env" {
  description = "Environment name (dev, prod)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the workbook and the alert rules."
  type        = string

  validation {
    condition     = length(var.resource_group_name) > 0
    error_message = "resource_group_name must not be empty."
  }
}

variable "resource_group_id" {
  description = "Resource group ID; scope of the role-assignment activity-log alerts."
  type        = string

  validation {
    condition     = can(regex("(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+$", var.resource_group_id))
    error_message = "resource_group_id must be a resource group ID: /subscriptions/<guid>/resourceGroups/<name>."
  }
}

variable "location" {
  description = "Azure region of the workbook and the scheduled query rules."
  type        = string

  validation {
    condition     = length(var.location) > 0
    error_message = "location must not be empty."
  }
}

variable "action_group_id" {
  description = "Resource ID of the existing email action group (modules/budget output action_group_id). All alerts notify it; no second group is created."
  type        = string

  validation {
    condition     = can(regex("(?i)/providers/microsoft\\.insights/actionGroups/[^/]+$", var.action_group_id))
    error_message = "action_group_id must be an action group resource ID."
  }
}

variable "app_insights_id" {
  description = "Application Insights resource ID (modules/log_analytics output app_insights_id). Scope of the API 5xx rule and the source of the workbook KQL."
  type        = string

  validation {
    condition     = can(regex("(?i)/providers/microsoft\\.insights/components/[^/]+$", var.app_insights_id))
    error_message = "app_insights_id must be an Application Insights resource ID."
  }
}

variable "workspace_id" {
  description = "Log Analytics workspace resource ID (modules/log_analytics output workspace_id). Scope of the freshness, daily-cap, spot-eviction and blob-audit rules."
  type        = string

  validation {
    condition     = can(regex("(?i)/providers/microsoft\\.operationalinsights/workspaces/[^/]+$", var.workspace_id))
    error_message = "workspace_id must be a Log Analytics workspace resource ID."
  }
}

variable "servicebus_namespace_id" {
  description = "Service Bus namespace ID (modules/servicebus output id). Scope of the dead-letter metric alert and the queue panels."
  type        = string

  validation {
    condition     = can(regex("(?i)/providers/microsoft\\.servicebus/namespaces/[^/]+$", var.servicebus_namespace_id))
    error_message = "servicebus_namespace_id must be a Service Bus namespace resource ID."
  }
}

variable "storage_account_id" {
  description = "Storage account ID (modules/storage output id). Scope of the management-policy alert, the blob audit diagnostic setting and the capacity panel."
  type        = string

  validation {
    condition     = can(regex("(?i)/providers/microsoft\\.storage/storageAccounts/[^/]+$", var.storage_account_id))
    error_message = "storage_account_id must be a storage account resource ID."
  }
}

variable "sql_database_id" {
  description = "SQL database ID (modules/sql output database_id) for the SQL space panel. null omits the panel."
  type        = string
  default     = null

  validation {
    condition     = var.sql_database_id == null || can(regex("(?i)/providers/microsoft\\.sql/servers/[^/]+/databases/[^/]+$", coalesce(var.sql_database_id, "x")))
    error_message = "sql_database_id must be a SQL database resource ID or null."
  }
}

variable "container_insights_enabled" {
  description = "True when the AKS cluster ships Container Insights tables (KubeNodeInventory, KubeEvents, KubePodInventory) to the workspace (modules/aks enable_container_insights). The spot-eviction alert and the KEDA/node panels need them: a log alert on a missing table fails at creation, so the alert is only created when this is true."
  type        = bool
  default     = false
}

variable "enable_blob_audit" {
  description = "Send blob StorageWrite and StorageDelete logs to the workspace and alert on SetBlobTags/DeleteBlob by callers not in expected_blob_modifier_object_ids (closes the T-207 note; the activity log does not carry data-plane calls). Costs Log Analytics ingestion (docs/finops/deltas/T-506.md)."
  type        = bool
  default     = true
}

variable "expected_blob_modifier_object_ids" {
  description = "Entra object IDs (principal IDs) allowed to set blob tags or delete blobs, normally id-tiering and id-ingest. Any other attributable caller raises the blob audit alert."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.expected_blob_modifier_object_ids : can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", o))])
    error_message = "expected_blob_modifier_object_ids must be GUIDs."
  }
}

variable "enable_ingest_freshness_alert" {
  description = "Enable the ingest freshness rule. Default false: enable once ingest calls configure_telemetry and record_rows_written (follow-up), otherwise no datapoint ever arrives and the rule pages permanently."
  type        = bool
  default     = false
}

variable "api_5xx_threshold_percent" {
  description = "Alert when the share of 5xx responses in the 10 minute window exceeds this percentage (SPEC-10 section 3: 2)."
  type        = number
  default     = 2

  validation {
    condition     = var.api_5xx_threshold_percent > 0 && var.api_5xx_threshold_percent <= 100
    error_message = "api_5xx_threshold_percent must be in (0, 100]."
  }
}

variable "api_5xx_min_requests" {
  description = "Minimum requests in the window before the 5xx ratio counts, so one failed request on an idle API does not page (dev API traffic is near zero)."
  type        = number
  default     = 20

  validation {
    condition     = var.api_5xx_min_requests >= 1 && floor(var.api_5xx_min_requests) == var.api_5xx_min_requests
    error_message = "api_5xx_min_requests must be a whole number of at least 1."
  }
}

variable "ingest_freshness_hours" {
  description = "Alert when no tidal.ingest.rows_written datapoint with a positive value arrived for this many hours, evaluated on weekdays only (SPEC-10 section 3: 36)."
  type        = number
  default     = 36

  validation {
    condition     = var.ingest_freshness_hours >= 1 && var.ingest_freshness_hours <= 47 && floor(var.ingest_freshness_hours) == var.ingest_freshness_hours
    error_message = "ingest_freshness_hours must be a whole number between 1 and 47 (the rule looks back 2 days)."
  }
}

variable "spot_eviction_threshold_percent" {
  description = "Alert when more than this percentage of the spot nodes seen in the last hour disappeared without an autoscaler scale-down (SPEC-10 section 3: 30)."
  type        = number
  default     = 30

  validation {
    condition     = var.spot_eviction_threshold_percent > 0 && var.spot_eviction_threshold_percent <= 100
    error_message = "spot_eviction_threshold_percent must be in (0, 100]."
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
