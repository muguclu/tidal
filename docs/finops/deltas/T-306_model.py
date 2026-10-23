# ruff: noqa: T201, E501, PLR2004  # standalone report script: prints the delta tables
"""Deterministic cost model behind docs/finops/deltas/T-306.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-306_model.py [docs/finops/prices.yaml]

Every EUR figure in the delta is printed here. Unit prices are read from prices.yaml; nothing is typed in.

Assumptions (stated, not measured):
- A month is 730 hours (the convention in the prices.yaml notes).
- Control plane: Free tier, no meter, 0.
- System pool: 1x Standard_B2s, on 730 h unless the cluster is stopped. OS disk 32 GB = P4 premium SSD (a managed
  OS disk, billed per month, also while the cluster is stopped because disks are not deleted by `az aks stop`).
- Spot pool: D4as_v5 spot at the list spot price (real price varies, evictions possible), scale to zero between
  sweeps. Each node has a 32 GB managed OS disk (P4; Dasv5 has no cache, so no ephemeral disk), billed prorated per
  node-hour as the monthly P4 meter / 730. Node-hours per month are SCENARIOS, not measurements:
  light = 4 sweeps x 10 nodes x 1 h, heavy = 10 sweeps x 10 nodes x 1.5 h. Node start-up time is ignored.
- Networking: the AKS standard load balancer (outbound) with one managed static public IP, both billed per hour for
  730 h. Whether `az aks stop` releases them is UNVERIFIED, so the parked column keeps them (conservative).
- Not priced (no meter in prices.yaml): load balancer data processed, outbound data transfer over 100 GB, the
  Container insights ingestion (off), API server and etcd (Free tier, included), Kubernetes control-plane logs (off).
"""

import sys
from pathlib import Path

import yaml

HOURS = 730
path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
with path.open(encoding="utf-8") as fh:
    P = {p["key"]: p["unitPrice"] for p in yaml.safe_load(fh)["prices"]}

B2S = P["vm_b2s_hour"]
SPOT = P["vm_d4as_v5_spot_hour"]
OND = P["vm_d4as_v5_hour"]
P4 = P["disk_premium_p4_month"]
LB = P["lb_standard_rules_hour"]
PIP = P["publicip_standard_static_hour"]

SPOT_DISK_HOUR = P4 / HOURS  # prorated managed OS disk per spot node-hour

SCENARIOS = {"light": 4 * 10 * 1.0, "heavy": 10 * 10 * 1.5}  # spot node-hours per month

rows = [
    ("Control plane (Free tier)", "none", 0.0, 0.0, 0.0),
    ("System pool 1x B2s", f"{HOURS} h x {B2S}", B2S * HOURS, 0.0, 0.0),
    ("System OS disk 32 GB (P4)", f"1 x {P4}", P4, P4, 0.0),
    ("Standard LB (outbound rules)", f"{HOURS} h x {LB}", LB * HOURS, LB * HOURS, 0.0),
    ("Public IP (outbound, static)", f"{HOURS} h x {PIP}", PIP * HOURS, PIP * HOURS, 0.0),
]
fixed_active = sum(r[2] for r in rows)
fixed_parked = sum(r[3] for r in rows)

print("| Component | Quantity x unit price (EUR) | Active EUR/month | Parked EUR/month |")
print("|---|---|---:|---:|")
for name, qty, act, park, _ in rows:
    print(f"| {name} | {qty} | {act:.2f} | {park:.2f} |")
print(f"| Spot pool D4as_v5 + 32 GB P4 OS disk | node-hours x ({SPOT} + {P4}/{HOURS}); scenarios below | 0.00 at idle | 0.00 |")
print(f"| **Fixed subtotal (no sweeps)** | | **{fixed_active:.2f}** | **{fixed_parked:.2f}** |")
print()
print("| Spot scenario | Node-hours/month | Spot compute EUR | OS disks EUR | Spot total EUR | Same hours on demand incl. disk EUR | Saving EUR | Total active EUR/month |")
print("|---|---:|---:|---:|---:|---:|---:|---:|")
for name, nh in SCENARIOS.items():
    c, d = nh * SPOT, nh * SPOT_DISK_HOUR
    o = nh * OND + d
    print(f"| {name} | {nh:.0f} | {c:.2f} | {d:.2f} | {c + d:.2f} | {o:.2f} | {o - c - d:.2f} | {fixed_active + c + d:.2f} |")
print()
print("| Variant | EUR/month |")
print("|---|---:|")
print(f"| Always-on spot node (min_count = 1) instead of 0 | {SPOT * HOURS + P4:.2f} (extra vs scale to zero, compute plus disk) |")
print(f"| SPEC-09 C4 baseline, compute only: 5x D4as_v5 on demand 24/7 | {5 * OND * HOURS:.2f} |")
