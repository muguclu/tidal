# aks

One AKS cluster `aks-tidal-<env>` for the backtest fan-out (SPEC-07 section 2, ADR-0002):

- **Free tier** control plane; system pool `system`, 1x `Standard_B2s` (fixed count, `CriticalAddonsOnly` taint).
- User pool `spot`: `Standard_D4as_v5`, Spot, **min 0 / max 10**, `spot_max_price = -1`, eviction `Delete`, 32 GB managed OS disk (P4; Dasv5 has no cache or temp disk, so ephemeral OS disks are not supported).
  Label `tidal.io/workload=backtest` and taint `tidal.io/workload=backtest:NoSchedule`; AKS adds its own
  `kubernetes.azure.com/scalesetpriority=spot:NoSchedule` taint. Backtest pods (T-307) must tolerate both taints and select the label (output `spot_pool_selector`).
- **KEDA add-on** (`workload_autoscaler_profile.keda_enabled`), **OIDC issuer** and **workload identity** on, Secrets Store CSI driver with rotation (SPEC-06 section 3). SecretProviderClass objects must use workload identity; the CSI add-on identity gets no role assignments.
- **Azure CNI Overlay** with the cilium data plane (the NetworkPolicy enforcement engine; the default-deny + egress 443 policy ships with the T-307 Helm chart); AKS-managed VNet (pass `vnet_subnet_id` once T-404 provides `snet-aks`).
- **Security:** local accounts disabled, Entra ID integration with Azure RBAC for Kubernetes, system-assigned cluster identity. Cluster admins are `azurerm_role_assignment`s of *Azure Kubernetes Service RBAC Cluster Admin* scoped to the cluster (`cluster_admin_principal_ids`); with local accounts off, an empty set means nobody can administer it.
- **Federated credentials** (`workload_identities`): one per Kubernetes service account, issuer = the cluster OIDC URL, audience `api://AzureADTokenExchange`, subject `system:serviceaccount:<ns>:<sa>` (SPEC-06 section 1). The dev composition creates `worker` -> `id-worker` for `tidal/tidal-worker`; the Helm chart (T-307) must use that service account name. Role assignments of the identities and the KEDA operator are T-403.
- **AcrPull** for the kubelet identity is not in this module: `envs/dev/aks.tf` scopes it to the registry from the `kubelet_identity_object_id` output (SPEC-06 section 2).
- **Log Analytics:** Container insights is off by default (`enable_container_insights`); it would exhaust the 0.2 GB/day workspace cap. Set it together with `log_analytics_workspace_id` if you accept that.
- Cost: see `docs/finops/deltas/T-306.md` (system node dominates; spot nodes cost only while sweeps run).

## Park and unpark (`az aks stop`)

The module is designed so that parking does not need Terraform changes:

- The system pool is a fixed single node and the spot pool is autoscaled with min 0, so the cluster is already at one node when idle.
- `az aks stop -g rg-tidal-dev -n aks-tidal-dev` deallocates all nodes and stops the control plane; `az aks start` brings it back. The cluster object, node pool definitions, identities, federated credentials and role assignments are kept, so nothing needs re-applying. Pool settings and the OIDC issuer URL are unchanged by stop/start.
- A stopped cluster still pays for the OS disk, and probably for the load balancer and public IP in the `MC_*` group (see the cost delta; UNVERIFIED until the first park test, T-313).
- UNVERIFIED: whether AKS accepts `az aks stop` while a Spot pool exists. If it refuses, the park script (T-313) should run `az aks nodepool scale`/delete on `spot` first (the pool is at 0 nodes when idle) or accept destroy as the stop.
- Terraform and a stopped cluster: reads work, updates may not. Run `az aks start` before `terraform apply`. The `park` tag flips through `terraform apply` (`make park`); apply it after the start, or run the tag update before stopping.
- Access: `az aks get-credentials -g rg-tidal-dev -n aks-tidal-dev` then `kubelogin convert-kubeconfig -l azurecli`.

## Checkov skips

Each skip carries its justification inline in `main.tf` (Free tier, public API server with Entra-only auth, no Azure Policy add-on, no CMK disk encryption, no host encryption, ephemeral OS disks unsupported on B2s and Dasv5, Container insights opt-in).

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |

## Resources

| Name | Type |
|------|------|
| `azurerm_kubernetes_cluster.this` | resource |
| `azurerm_kubernetes_cluster_node_pool.spot` | resource |
| `azurerm_role_assignment.cluster_admin` | resource |
| `azurerm_federated_identity_credential.workload` | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `env` | Environment name (dev, prod). | `string` | `"dev"` | no |
| `resource_group_name` | Resource group for the cluster. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `tenant_id` | Entra tenant ID. | `string` | n/a | yes |
| `kubernetes_version` | Kubernetes version, null for the AKS default. | `string` | `null` | no |
| `automatic_upgrade_channel` | Upgrade channel. | `string` | `"patch"` | no |
| `system_pool_vm_size` | System node size. | `string` | `"Standard_B2s"` | no |
| `system_pool_os_disk_size_gb` | System OS disk size (32 = P4). | `number` | `32` | no |
| `spot_pool_vm_size` | Spot node size. | `string` | `"Standard_D4as_v5"` | no |
| `spot_pool_min_count` | Minimum spot nodes. | `number` | `0` | no |
| `spot_pool_max_count` | Maximum spot nodes. | `number` | `10` | no |
| `spot_pool_os_disk_size_gb` | Managed OS disk size of spot nodes. | `number` | `32` | no |
| `workload_label` | Value of the tidal.io/workload label and taint. | `string` | `"backtest"` | no |
| `pod_cidr` | Overlay pod CIDR. | `string` | `"10.244.0.0/16"` | no |
| `service_cidr` | Service CIDR. | `string` | `"10.0.0.0/16"` | no |
| `dns_service_ip` | Cluster DNS IP inside service_cidr. | `string` | `"10.0.0.10"` | no |
| `vnet_subnet_id` | Optional subnet ID for the node pools. | `string` | `null` | no |
| `api_server_authorized_ip_ranges` | CIDRs allowed to reach the API server. | `list(string)` | `[]` | no |
| `cluster_admin_principal_ids` | Object IDs that get AKS RBAC Cluster Admin. | `set(string)` | `[]` | no |
| `enable_container_insights` | Enable the oms_agent add-on. | `bool` | `false` | no |
| `log_analytics_workspace_id` | Workspace for Container insights. | `string` | `null` | no |
| `workload_identities` | Name to { identity_id, namespace, service_account } federated credentials. | `map(object)` | `{}` | no |
| `tags` | Tag map from `module.resource_group.tags`. | `map(string)` | n/a | yes |
| `component` | Value of the component tag. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| `id` | Cluster resource ID. |
| `name` | Cluster name. |
| `oidc_issuer_url` | OIDC issuer URL for federated credentials. |
| `kubelet_identity_object_id` | Kubelet identity object ID (AcrPull). |
| `node_resource_group` | MC_* node resource group. |
| `spot_node_pool_name` | Spot pool name. |
| `spot_pool_selector` | Label and taint for backtest pods. |
<!-- END_TF_DOCS -->
