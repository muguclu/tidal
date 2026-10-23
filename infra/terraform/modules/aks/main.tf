locals {
  tags         = merge(var.tags, { component = var.component })
  cluster_name = "aks-tidal-${var.env}"

  # The spot pool is tainted so that only backtest pods land on it. AKS adds
  # kubernetes.azure.com/scalesetpriority=spot:NoSchedule itself for spot pools; it must not be listed here.
  spot_taints = ["tidal.io/workload=${var.workload_label}:NoSchedule"]
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = local.cluster_name
  resource_group_name = var.resource_group_name
  location            = var.location
  dns_prefix          = local.cluster_name
  kubernetes_version  = var.kubernetes_version

  # Checkov skips (each justified):
  #checkov:skip=CKV_AZURE_170:Free tier control plane is the SPEC-07 section 2 cost decision; no uptime SLA is needed for a dev portfolio cluster
  #checkov:skip=CKV_AZURE_115:Private cluster needs a VNet, private DNS and a jump path; deferred to the private-networking phase (T-404, SPEC-06 section 4). Mitigation: Entra ID + Azure RBAC only, local accounts disabled, optional authorized IP ranges
  #checkov:skip=CKV_AZURE_6:Authorized IP ranges are an opt-in variable (api_server_authorized_ip_ranges) because a dev laptop IP is not known to the module; Entra ID auth is always required
  #checkov:skip=CKV_AZURE_4:Container insights is opt-in (enable_container_insights): it would exhaust the 0.2 GB/day Log Analytics cap (SPEC-07 section 2)
  #checkov:skip=CKV_AZURE_116:Azure Policy add-on adds a gatekeeper footprint on a 4 GB system node; admission rules are enforced by the Helm chart and NetworkPolicy instead
  #checkov:skip=CKV_AZURE_117:A disk encryption set (customer-managed key) needs Key Vault key handling and a DES; platform-managed encryption at rest is accepted for synthetic data (ADR-0013)
  #checkov:skip=CKV_AZURE_227:Encryption at host needs a subscription feature flag and is not offered for B-series; platform-managed encryption at rest is accepted for synthetic data
  #checkov:skip=CKV_AZURE_226:The B2s system node has no VM cache for an ephemeral OS disk; the spot pool, which holds the bulk of the nodes, does use an ephemeral OS disk
  sku_tier = "Free"

  # Entra ID only: no static admin kubeconfig. Access is Azure RBAC for Kubernetes (cluster_admin_principal_ids).
  local_account_disabled            = true
  role_based_access_control_enabled = true

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = var.tenant_id
  }

  # Workload identity (SPEC-06 section 1): pods exchange a projected service-account token for a Managed Identity token.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  automatic_upgrade_channel = var.automatic_upgrade_channel
  node_os_upgrade_channel   = "NodeImage"

  # Cluster identity: system-assigned. Its kubelet identity (output kubelet_identity_object_id) gets AcrPull on the
  # registry; that role assignment lives next to the registry composition (envs/dev/aks.tf).
  identity {
    type = "SystemAssigned"
  }

  default_node_pool {
    name                         = "system"
    vm_size                      = var.system_pool_vm_size
    node_count                   = 1 # fixed: one node, no autoscaler, so `az aks stop` and cost are predictable
    auto_scaling_enabled         = false
    os_disk_size_gb              = var.system_pool_os_disk_size_gb
    os_disk_type                 = "Managed"
    only_critical_addons_enabled = true # keeps workloads (and spot evictions) away from the only system node
    vnet_subnet_id               = var.vnet_subnet_id
    max_pods                     = 50
    temporary_name_for_rotation  = "systemtmp"
    tags                         = local.tags

    upgrade_settings {
      max_surge = "10%"
    }
  }

  # Azure CNI Overlay (SPEC-07 section 2): pod IPs come from an overlay range, not from the VNet.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium" # NetworkPolicy is enforced (deny-all + allow egress 443, SPEC-07 section 4)
    pod_cidr            = var.pod_cidr
    service_cidr        = var.service_cidr
    dns_service_ip      = var.dns_service_ip
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"
  }

  # KEDA add-on (SPEC-07 section 2): scales the backtest ScaledJob from 0 on Service Bus queue depth.
  workload_autoscaler_profile {
    keda_enabled = true
  }

  # Secrets Store CSI driver (SPEC-06 section 3): AKS reads Key Vault through it.
  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }

  dynamic "api_server_access_profile" {
    for_each = length(var.api_server_authorized_ip_ranges) > 0 ? [1] : []
    content {
      authorized_ip_ranges = var.api_server_authorized_ip_ranges
    }
  }

  dynamic "oms_agent" {
    for_each = var.enable_container_insights ? [1] : []
    content {
      log_analytics_workspace_id      = var.log_analytics_workspace_id
      msi_auth_for_monitoring_enabled = true
    }
  }

  tags = local.tags

  lifecycle {
    precondition {
      condition     = !var.enable_container_insights || var.log_analytics_workspace_id != null
      error_message = "enable_container_insights needs log_analytics_workspace_id."
    }
    precondition {
      condition     = var.spot_pool_min_count <= var.spot_pool_max_count
      error_message = "spot_pool_min_count must not exceed spot_pool_max_count."
    }
  }
}

# Spot user pool for backtest fan-out (SPEC-07 section 2): autoscaled 0..N, evicted nodes are deleted, price cap = pay
# up to the on-demand price. Only pods that tolerate the tidal.io/workload taint run here.
resource "azurerm_kubernetes_cluster_node_pool" "spot" {
  name                  = "spot"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  mode                  = "User"

  #checkov:skip=CKV_AZURE_227:Encryption at host needs a subscription feature flag (EncryptionAtHost) that dev subscriptions do not have by default; platform-managed encryption at rest is accepted for synthetic data
  vm_size        = var.spot_pool_vm_size
  vnet_subnet_id = var.vnet_subnet_id

  priority        = "Spot"
  eviction_policy = "Delete"
  spot_max_price  = -1

  auto_scaling_enabled = true
  min_count            = var.spot_pool_min_count
  max_count            = var.spot_pool_max_count

  # Ephemeral OS disk on the VM cache: no managed-disk charge and faster scale-up.
  os_disk_type    = "Ephemeral"
  os_disk_size_gb = var.spot_pool_os_disk_size_gb
  max_pods        = 50

  node_labels = {
    "tidal.io/workload" = var.workload_label
  }
  node_taints = local.spot_taints

  upgrade_settings {
    max_surge = "10%"
  }

  tags = local.tags

  lifecycle {
    # The cluster autoscaler owns the live node count.
    ignore_changes = [node_count]
  }
}

# Cluster administration: Azure RBAC for Kubernetes, scoped to this cluster only.
resource "azurerm_role_assignment" "cluster_admin" {
  for_each = var.cluster_admin_principal_ids

  scope                = azurerm_kubernetes_cluster.this.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = each.value
}

# One federated credential per Kubernetes service account (SPEC-06 section 1).
resource "azurerm_federated_identity_credential" "workload" {
  for_each = var.workload_identities

  name      = each.key
  parent_id = each.value.identity_id
  issuer    = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject   = "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
  audience  = ["api://AzureADTokenExchange"]
}
