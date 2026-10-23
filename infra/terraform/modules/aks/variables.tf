variable "env" {
  description = "Environment name; part of the cluster name (aks-tidal-<env>)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}

variable "resource_group_name" {
  description = "Resource group that holds the cluster."
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

variable "tenant_id" {
  description = "Entra tenant ID used for Entra ID integration (Azure RBAC for Kubernetes)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.tenant_id))
    error_message = "tenant_id must be a GUID."
  }
}

variable "kubernetes_version" {
  description = "Kubernetes minor or patch version (for example 1.33). null lets AKS pick its current default for new clusters; upgrades then follow automatic_upgrade_channel."
  type        = string
  default     = null

  validation {
    condition     = var.kubernetes_version == null || can(regex("^[0-9]+\\.[0-9]+(\\.[0-9]+)?$", coalesce(var.kubernetes_version, "0.0")))
    error_message = "kubernetes_version must be null or look like 1.33 or 1.33.2."
  }
}

variable "automatic_upgrade_channel" {
  description = "AKS automatic upgrade channel. patch keeps the minor version fixed and applies security patches."
  type        = string
  default     = "patch"

  validation {
    condition     = contains(["none", "patch", "stable", "rapid", "node-image"], var.automatic_upgrade_channel)
    error_message = "automatic_upgrade_channel must be one of: none, patch, stable, rapid, node-image."
  }
}

variable "system_pool_vm_size" {
  description = "VM size of the single system node (SPEC-07 section 2: Standard_B2s)."
  type        = string
  default     = "Standard_B2s"

  validation {
    condition     = can(regex("^Standard_[A-Za-z0-9_]+$", var.system_pool_vm_size))
    error_message = "system_pool_vm_size must look like Standard_B2s."
  }
}

variable "system_pool_os_disk_size_gb" {
  description = "OS disk size of the system node in GB. 32 maps to the P4 managed disk tier (the smallest size above the AKS minimum of 30 GB)."
  type        = number
  default     = 32

  validation {
    condition     = var.system_pool_os_disk_size_gb >= 30 && var.system_pool_os_disk_size_gb <= 128
    error_message = "system_pool_os_disk_size_gb must be between 30 and 128."
  }
}

variable "spot_pool_vm_size" {
  description = "VM size of the spot user pool (SPEC-07 section 2: Standard_D4as_v5)."
  type        = string
  default     = "Standard_D4as_v5"

  validation {
    condition     = can(regex("^Standard_[A-Za-z0-9_]+$", var.spot_pool_vm_size))
    error_message = "spot_pool_vm_size must look like Standard_D4as_v5."
  }
}

variable "spot_pool_min_count" {
  description = "Minimum spot nodes. 0 is the scale-to-zero setting (SPEC-07 section 2) and should stay 0."
  type        = number
  default     = 0

  validation {
    condition     = var.spot_pool_min_count >= 0 && var.spot_pool_min_count <= 10 && floor(var.spot_pool_min_count) == var.spot_pool_min_count
    error_message = "spot_pool_min_count must be a whole number from 0 to 10."
  }
}

variable "spot_pool_max_count" {
  description = "Maximum spot nodes (SPEC-07 section 2: 10). Each D4as_v5 node is 4 vCPU, so 10 nodes need 40 vCPU of Standard Dasv5 spot quota in the region; request quota first or lower this value."
  type        = number
  default     = 10

  validation {
    condition     = var.spot_pool_max_count >= 1 && var.spot_pool_max_count <= 100 && floor(var.spot_pool_max_count) == var.spot_pool_max_count
    error_message = "spot_pool_max_count must be a whole number from 1 to 100."
  }
}

variable "spot_pool_os_disk_size_gb" {
  description = "Size of the ephemeral OS disk of spot nodes in GB. Ephemeral disks live on the VM cache (100 GB on D4as_v5), so they have no disk charge; the size must fit the cache."
  type        = number
  default     = 64

  validation {
    condition     = var.spot_pool_os_disk_size_gb >= 30 && var.spot_pool_os_disk_size_gb <= 100
    error_message = "spot_pool_os_disk_size_gb must be between 30 and 100 (D4as_v5 cache is 100 GB)."
  }
}

variable "workload_label" {
  description = "Value of the node label and taint tidal.io/workload on the spot pool. Backtest ScaledJob pods tolerate the taint and select the label (T-307)."
  type        = string
  default     = "backtest"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", var.workload_label))
    error_message = "workload_label must be a valid Kubernetes label value (lowercase alphanumerics and dashes)."
  }
}

variable "pod_cidr" {
  description = "Pod CIDR of Azure CNI Overlay. Overlay pod IPs are not routable in the VNet, so this range only has to avoid overlap with peered networks."
  type        = string
  default     = "10.244.0.0/16"

  validation {
    condition     = can(cidrhost(var.pod_cidr, 0))
    error_message = "pod_cidr must be a valid CIDR block."
  }
}

variable "service_cidr" {
  description = "Kubernetes service CIDR. Must not overlap pod_cidr or any peered VNet."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.service_cidr, 0))
    error_message = "service_cidr must be a valid CIDR block."
  }
}

variable "dns_service_ip" {
  description = "Cluster DNS service IP; must be inside service_cidr (conventionally the .10 address)."
  type        = string
  default     = "10.0.0.10"

  validation {
    condition     = can(cidrhost("${var.dns_service_ip}/32", 0))
    error_message = "dns_service_ip must be a valid IPv4 address."
  }
}

variable "vnet_subnet_id" {
  description = "Optional subnet ID (snet-aks) for the node pools once the network module exists (T-404). null uses the AKS-managed VNet, which Azure CNI Overlay supports in dev."
  type        = string
  default     = null
}

variable "api_server_authorized_ip_ranges" {
  description = "CIDRs allowed to reach the public API server. Empty means no restriction (Entra ID auth, local accounts disabled and Azure RBAC still apply). Set this to your own public IP when you can."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.api_server_authorized_ip_ranges : can(cidrhost(c, 0))])
    error_message = "api_server_authorized_ip_ranges must contain valid CIDR blocks."
  }
}

variable "cluster_admin_principal_ids" {
  description = "Object IDs (users, groups or service principals) that receive Azure Kubernetes Service RBAC Cluster Admin on this cluster. Local accounts are disabled, so without at least one entry nobody can administer the cluster."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.cluster_admin_principal_ids : can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", id))])
    error_message = "cluster_admin_principal_ids must contain GUIDs."
  }
}

variable "enable_container_insights" {
  description = "Send container logs and metrics to Log Analytics (oms_agent). Off by default: it would consume the 0.2 GB/day workspace cap (SPEC-07 section 2). Needs log_analytics_workspace_id."
  type        = bool
  default     = false
}

variable "log_analytics_workspace_id" {
  description = "Resource ID of the Log Analytics workspace for Container insights. Only used when enable_container_insights is true."
  type        = string
  default     = null
}

variable "workload_identities" {
  description = "Federated credentials that let a Kubernetes service account act as a user-assigned managed identity (SPEC-06 section 1: one credential per service account). Map key is the credential name."
  type = map(object({
    identity_id     = string
    namespace       = string
    service_account = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for k, v in var.workload_identities :
      can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$", k)) && length(v.namespace) > 0 && length(v.service_account) > 0
    ])
    error_message = "workload_identities keys must be valid credential names and namespace and service_account must not be empty."
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
  description = "Value of the component tag for the resources in this module (for example compute); merged over tags[\"component\"] inside the module."
  type        = string

  validation {
    condition     = length(trimspace(var.component)) > 0
    error_message = "component must not be empty."
  }
}
