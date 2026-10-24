# ruff: noqa: T201, E501  # standalone report script: prints the delta tables
"""Deterministic cost model behind docs/finops/deltas/T-404.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-404_model.py [docs/finops/prices.yaml]

Unit prices are read from prices.yaml; nothing is typed in except the usage SCENARIO values below.

Scope: the cost of ``enable_private_networking = true`` on top of the flag-off environment. Flag off costs 0.00
(the network module creates no resource).

Assumptions (stated, not measured):
- A month is 730 hours (convention in the prices.yaml notes).
- Three private endpoints (Blob, SQL, Key Vault) bill per endpoint-hour, 24/7, and do NOT stop when the environment is
  parked (``make park`` deallocates compute; endpoints, zones and the outbound IP stay). Only flag off / destroy removes them.
- Private Link data processed: PE_GB_PER_MONTH GB through all endpoints, billed on both ingress and egress meters.
  Dev traffic is the cold Parquet reads of the API, jobs and the daily export plus SQL hot reads. 20 GB is an assumption
  until the first Cost Management export; the sensitivity table shows 100 GB.
- Private DNS: 3 zones for the endpoints plus 1 zone for the internal Container Apps environment (4 zones, all inside the
  first 25 zones tier), DNS_QUERIES_M million queries per month (assumption).
- snet-vm outbound: ``public_ip`` mode = one Standard static IPv4 (default); ``nat_gateway`` mode = NAT gateway + the same
  IP + NAT_GB_PER_MONTH GB processed (ARM, Entra, PyPI/apt on first boot: well under 1 GB).
- Free: VNet, subnets, NSGs, service endpoint, App Service regional VNet integration (B1), private DNS records.
- Not priced (no meter or unknown): the managed load balancer of an internal Container Apps environment. Its billing is
  not documented clearly for Consumption-only environments, so it is shown as an upper-bound risk using the Standard
  LB rule meter, outside the subtotal. Verify in the first Cost Management export after the flag is switched on.
"""

import sys
from pathlib import Path

import yaml

HOURS = 730
PE_COUNT = 3
PE_GB_PER_MONTH = 20.0
PE_GB_SENSITIVITY = 100.0
DNS_ZONES = 4
DNS_QUERIES_M = 1.0
NAT_GB_PER_MONTH = 0.1

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
with path.open(encoding="utf-8") as fh:
    P = {p["key"]: p["unitPrice"] for p in yaml.safe_load(fh)["prices"]}

PE_H = P["private_endpoint_hour"]
PE_IN = P["private_link_data_ingress_gb"]
PE_OUT = P["private_link_data_egress_gb"]
ZONE = P["private_dns_zone_month"]
QUERIES = P["private_dns_queries_1m"]
PIP = P["publicip_standard_static_hour"]
NAT_H = P["nat_gateway_hour"]
NAT_GB = P["nat_gateway_data_gb"]
LB_H = P["lb_standard_rules_hour"]


def pe_data(gb: float) -> float:
    """Private Link data-processed cost for ``gb`` GB (ingress plus egress meters)."""
    return gb * (PE_IN + PE_OUT)


pe_fixed = PE_COUNT * PE_H * HOURS
pe_traffic = pe_data(PE_GB_PER_MONTH)
dns_zones = DNS_ZONES * ZONE
dns_queries = DNS_QUERIES_M * QUERIES
out_pip = PIP * HOURS
out_nat = (NAT_H + PIP) * HOURS + NAT_GB_PER_MONTH * NAT_GB

common = pe_fixed + pe_traffic + dns_zones + dns_queries
total_pip = common + out_pip
total_nat = common + out_nat

print("| Component | Quantity x unit price (EUR) | Active EUR/month | Parked EUR/month |")
print("|---|---|---:|---:|")
print(
    f"| Private endpoints (Blob, SQL, Key Vault) | {PE_COUNT} x {HOURS} h x {PE_H} | {pe_fixed:.2f} | {pe_fixed:.2f} |"
)
print(
    f"| Private Link data processed | {PE_GB_PER_MONTH:g} GB x ({PE_IN} + {PE_OUT}) | {pe_traffic:.2f} | 0.00 |"
)
print(f"| Private DNS zones | {DNS_ZONES} x {ZONE} | {dns_zones:.2f} | {dns_zones:.2f} |")
print(f"| Private DNS queries | {DNS_QUERIES_M:g} M x {QUERIES} | {dns_queries:.2f} | 0.00 |")
print(
    f"| snet-vm outbound: Standard static public IP (default) | {HOURS} h x {PIP} | {out_pip:.2f} | {out_pip:.2f} |"
)
print(
    f"| **Subtotal, vm_outbound = public_ip** | | **{total_pip:.2f}** | **{pe_fixed + dns_zones + out_pip:.2f}** |"
)
print()
print("Variant: vm_outbound = nat_gateway")
print()
print("| Component | Quantity x unit price (EUR) | EUR/month |")
print("|---|---|---:|")
print(f"| NAT gateway | {HOURS} h x {NAT_H} | {NAT_H * HOURS:.2f} |")
print(f"| Its Standard static public IP | {HOURS} h x {PIP} | {out_pip:.2f} |")
print(
    f"| NAT data processed | {NAT_GB_PER_MONTH:g} GB x {NAT_GB} | {NAT_GB_PER_MONTH * NAT_GB:.2f} |"
)
print(f"| NAT outbound subtotal | | {out_nat:.2f} |")
print(f"| **Subtotal, vm_outbound = nat_gateway** | | **{total_nat:.2f}** |")
print(f"| Delta NAT vs public IP | | +{out_nat - out_pip:.2f} |")
print()
print("Sensitivity and risks (not in the subtotal):")
print()
print("| Case | EUR/month |")
print("|---|---:|")
print(
    f"| Private Link traffic {PE_GB_SENSITIVITY:g} GB instead of {PE_GB_PER_MONTH:g} GB | +{pe_data(PE_GB_SENSITIVITY) - pe_traffic:.2f} |"
)
print(
    f"| Upper bound if the internal Container Apps load balancer is billed (Standard LB rule meter {LB_H}/h) | +{LB_H * HOURS:.2f} |"
)
print("| Flag off (default) | 0.00 |")
