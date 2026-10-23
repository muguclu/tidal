output "id" {
  description = "Resource ID of the AKS cluster."
  value       = azurerm_kubernetes_cluster.this.id
}

output "name" {
  description = "Cluster name (aks-tidal-<env>); used by az aks stop/start (park scripts, T-313) and az aks get-credentials."
  value       = azurerm_kubernetes_cluster.this.name
}

output "oidc_issuer_url" {
  description = "OIDC issuer URL of the cluster; the issuer of every federated identity credential (T-403 adds KEDA and further credentials)."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "kubelet_identity_object_id" {
  description = "Object ID of the kubelet managed identity; receives AcrPull on the registry (SPEC-06 section 2)."
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}

output "node_resource_group" {
  description = "Auto-created MC_* resource group with the nodes, load balancer and disks (covered by the subscription-scope budget)."
  value       = azurerm_kubernetes_cluster.this.node_resource_group
}

output "spot_node_pool_name" {
  description = "Name of the spot user pool."
  value       = azurerm_kubernetes_cluster_node_pool.spot.name
}

output "spot_pool_selector" {
  description = "Node label and taint that backtest pods must select and tolerate (consumed by the Helm chart, T-307)."
  value = {
    label_key   = "tidal.io/workload"
    label_value = var.workload_label
    taint_key   = "tidal.io/workload"
    taint       = "NoSchedule"
  }
}
