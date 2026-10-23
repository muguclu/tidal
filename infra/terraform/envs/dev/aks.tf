# AKS composition (T-306): free-tier cluster, B2s system pool, D4as_v5 spot pool scaling 0..10, KEDA, workload identity.
# Everything AKS-specific lives in this file so the shared main.tf, variables.tf and outputs.tf stay untouched.

variable "aks_extra_admin_object_ids" {
  description = "Entra object IDs that get Azure Kubernetes Service RBAC Cluster Admin on the dev cluster. Prefer an Entra admin group; the identity running terraform apply is added only as a bootstrap fallback."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.aks_extra_admin_object_ids : can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", id))])
    error_message = "aks_extra_admin_object_ids must contain GUIDs."
  }
}

variable "aks_spot_max_count" {
  description = "Maximum nodes of the AKS spot pool (SPEC-07: 10). Lower it if the subscription has less than 40 vCPU of Dasv5 spot quota."
  type        = number
  default     = 10

  validation {
    condition     = var.aks_spot_max_count >= 1 && var.aks_spot_max_count <= 100 && floor(var.aks_spot_max_count) == var.aks_spot_max_count
    error_message = "aks_spot_max_count must be a whole number from 1 to 100."
  }
}

variable "aks_api_server_authorized_ip_ranges" {
  description = "CIDRs allowed to reach the public AKS API server (for example [\"203.0.113.7/32\"]). Empty means unrestricted (Entra ID and Azure RBAC still apply)."
  type        = list(string)
  default     = []
}

module "aks" {
  source = "../../modules/aks"

  env                 = local.env
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  tags                = module.resource_group.tags
  component           = "compute"

  spot_pool_max_count             = var.aks_spot_max_count
  api_server_authorized_ip_ranges = var.aks_api_server_authorized_ip_ranges

  # Local accounts are disabled, so the applier and any extra admins get the cluster-scoped RBAC admin role.
  cluster_admin_principal_ids = toset(concat([data.azurerm_client_config.current.object_id], tolist(var.aks_extra_admin_object_ids)))

  # Container insights stays off: it would exhaust the 0.2 GB/day Log Analytics cap (docs/finops/deltas/T-306.md).
  enable_container_insights = false

  # Service account name contract for the Helm chart (T-307): namespace tidal, account tidal-worker.
  # Roles of id-worker are assigned in T-403; KEDA operator credentials are added there as well.
  workload_identities = {
    worker = {
      identity_id     = module.identity.identities["id-worker"].id
      namespace       = "tidal"
      service_account = "tidal-worker"
    }
  }
}

# SPEC-06 section 2: the kubelet identity pulls images, nothing else. Scoped to the registry, not the resource group.
resource "azurerm_role_assignment" "aks_kubelet_acr_pull" {
  scope                = module.acr.id
  role_definition_name = "AcrPull"
  principal_id         = module.aks.kubelet_identity_object_id

  # The kubelet identity is a managed identity created with the cluster; skip the AAD replication wait on a missing principal type.
  skip_service_principal_aad_check = true
}

output "aks_name" {
  description = "AKS cluster name; use with az aks get-credentials, az aks stop and az aks start."
  value       = module.aks.name
}

output "aks_oidc_issuer_url" {
  description = "OIDC issuer URL of the AKS cluster (issuer for federated credentials added in T-403)."
  value       = module.aks.oidc_issuer_url
}

output "aks_spot_pool_selector" {
  description = "Node label and taint that backtest pods select and tolerate (Helm chart, T-307)."
  value       = module.aks.spot_pool_selector
}
