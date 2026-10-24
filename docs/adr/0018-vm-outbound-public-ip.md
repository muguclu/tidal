# ADR-0018: Legacy VM outbound in private networking mode is a Standard public IP (dev deviation)

- Status: Accepted
- Date: 2026-10-24
- Deciders: Müge Güçlü (+ orchestrator, infra-engineer, security-reviewer)
- Related: SPEC-06 §4, ADR-0013, T-310, T-404, `docs/finops/deltas/T-404.md`

## Context
With `enable_private_networking = true` the legacy VM moves to `snet-vm` with default outbound access off. The exporter
needs HTTPS out to `management.azure.com` (self-deallocate), Entra, PyPI and apt. SPEC-06 §4 said "VM: no public IP".

## Options
1. **NAT gateway** (`network_vm_outbound = "nat_gateway"`). No address on the VM. About +28.91 EUR/month over option 2,
   billed 24/7 also while the VM is deallocated.
2. **One Standard static public IP on the NIC** (default). 3.21 EUR/month.
3. Azure Firewall or a proxy. Far more expensive than the VM it protects.

## Decision
Option 2 for dev. The address is outbound only: a Standard IP accepts no inbound traffic by default, two NSGs (the NIC NSG
in `vm_legacy` and the `snet-vm` subnet NSG) deny all inbound, there is no listener and no extension. A `terraform test`
asserts neither NSG has an Inbound/Allow rule. NAT would add about 29 EUR/month to protect a VM with about 0.05 EUR of
compute that handles only synthetic data (ADR-0013).

## Consequences
- SPEC-06 §4 now says "no inbound path" and records the deviation.
- Production answer: NAT gateway or a firewall with FQDN rules; set `network_vm_outbound = "nat_gateway"` to get the NAT variant.
- Residual risk (open egress from the VM) is listed in the network README for the T-406 threat model.
- Revisit if the VM ever handles non-synthetic data.
