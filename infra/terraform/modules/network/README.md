# network

Private networking behind one flag (`enabled`, wired to `enable_private_networking` in `envs/dev`, SPEC-06 section 4,
ADR-0006). **Flag off (default): zero resources, zero cost, every output null or empty.** Flag on: about 26 resources,
about 25 EUR/month (`docs/finops/deltas/T-404.md`).

| Subnet | CIDR (from 10.40.0.0/16) | Consumer |
|---|---|---|
| `snet-aks` | /24 | AKS node pools (`vnet_subnet_id`; Azure CNI Overlay keeps pods out of the VNet) |
| `snet-aca` | /23 | Container Apps environment (Consumption-only needs /23, no delegation) |
| `snet-app` | /27, delegated to `Microsoft.Web/serverFarms` | App Service B1 regional VNet integration |
| `snet-pe` | /27 | private endpoints for Blob, SQL, Key Vault |
| `snet-vm` | /28, default outbound off, `Microsoft.Storage` service endpoint | legacy VM NIC |

## What the flag does across the environment

- Storage, Key Vault, SQL: their `enable_private_networking` input sets `public_network_access_enabled = false` (Key Vault
  also `network_acls.default_action = Deny`); this module adds the endpoint, private DNS zone and VNet link for each.
- Service Bus stays public behind RBAC and TLS 1.2 (ADR-0006; a private endpoint needs Premium).
- tidal-query: the Container Apps environment becomes internal (`internal_load_balancer_enabled`) in `snet-aca`; its default
  domain is a private DNS zone linked to the VNet. App Service reaches it through VNet integration. The api to query hop
  stays **https** (`allow_insecure_connections = false`, `TIDAL_QUERY__BASE_URL = https://...`), terminated by the platform
  certificate; plain http inside the VNet is not enabled. The IP allow-list patch (`envs/dev/apps.tf`) is replaced by an empty
  list because the web app's source address in the VNet is private, not one of its public outbound IPs. The JWT caller
  check from T-402 remains the identity control.
- snet-vm: default outbound access is off. `vm_outbound = public_ip` (default) gives the VM NIC one Standard static IP
  (outbound only: Standard IPs accept no inbound, and the NIC NSG denies all inbound); `nat_gateway` keeps the VM address-less
  for about 29 EUR/month more. The VM NIC NSG gained an outbound allow to `VirtualNetwork:443` so the Blob private endpoint
  is reachable.

## Operational consequences (document, not hidden)

- **Terraform and CI reach.** With Blob and Key Vault closed, `terraform apply`, GitHub-hosted runners and an operator laptop
  cannot reach those data planes. Apply from a host inside the VNet (or switch the flag off for the apply). The deploy
  workflow (T-311) only pushes images to ACR (public, Basic SKU has no private link) and rolls app revisions through ARM, which
  is unaffected. The **deploy smoke tests (T-308/T-311 runbooks)**: the API (App Service) remains public so
  `/v1/health/live` from a runner still works, but any smoke test that calls tidal-query directly, reads Blob or queries SQL
  from a runner fails in private mode; run those from inside the VNet or skip them with the flag on.
- Switching the flag on an existing environment forces new AKS node pools and a new Container Apps environment.
- DNS: all private zones are linked to the one VNet; hosts outside it keep resolving public addresses (which are closed).
- App Service itself stays public (Entra JWT) by design; a private endpoint on the web app is a follow-up.

## Inputs and tests

`terraform test` (mock provider, no credentials) in `tests/network.tftest.hcl` checks flag off (0 resources, null outputs),
flag on with `public_ip` (26 resources) and with `nat_gateway`. Example: `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.9.0 |
| azurerm | ~> 4.0 |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| `enabled` | Master switch; false creates nothing. | `bool` | `false` | no |
| `env` | Environment name. | `string` | n/a | yes |
| `resource_group_name` | Resource group for all resources. | `string` | n/a | yes |
| `location` | Azure region. | `string` | `"westeurope"` | no |
| `address_space` | VNet /16, not overlapping 10.244.0.0/16 or 10.0.0.0/16. | `string` | `"10.40.0.0/16"` | no |
| `vm_outbound` | `public_ip` (default) or `nat_gateway`. | `string` | `"public_ip"` | no |
| `private_endpoint_targets` | Resource IDs keyed blob, sql, vault. | `map(string)` | `{}` | no |
| `container_apps_environment` | default_domain and static_ip_address of an internal environment. | `object` | `null` | no |
| `tags` | Tag map from module.resource_group.tags. | `map(string)` | n/a | yes |
| `component` | Value of the component tag. | `string` | `"network"` | no |

## Outputs

| Name | Description |
|------|-------------|
| `enabled` | True when the network exists. |
| `vnet_id` | VNet ID or null. |
| `subnet_ids` | Subnet IDs by short name or empty map. |
| `aks_subnet_id`, `aca_subnet_id`, `app_subnet_id`, `vm_subnet_id` | Individual subnet IDs or null. |
| `vm_public_ip_id` | Public IP for the VM NIC (public_ip mode) or null. |
| `private_endpoint_ids`, `private_dns_zone_ids` | Maps by service, empty when disabled. |
<!-- END_TF_DOCS -->
