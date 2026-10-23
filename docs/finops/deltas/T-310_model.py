# ruff: noqa: T201, E501  # standalone report script: prints the delta tables
"""Deterministic cost model behind docs/finops/deltas/T-310.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-310_model.py [docs/finops/prices.yaml]

Unit prices are read from prices.yaml; nothing is typed in except the usage SCENARIOS below.

Assumptions (stated, not measured):
- A month is 730 hours (the convention in the prices.yaml notes), 30 runs per month.
- The VM is Standard_B1s Linux (meter vm_b1s_hour). It is billed for compute only while allocated; when deallocated
  only the OS disk is billed. The OS disk is the 30 GiB Ubuntu image on StandardSSD_LRS, which bills as an E4 disk
  (32 GiB, meter disk_standard_ssd_e4_month); a smaller cheaper tier does not fit the image.
- Allocated time per run: Logic App starts the VM at 21:55, boot to systemd about 1 min, export about 2 min, deallocate
  call and shutdown about 2 min; modelled as RUN_MINUTES = 10 per day (assumption, to be replaced by the first C5 export
  of the VM meter). The first boot adds a pip install (about 5 min, once) and is ignored in the monthly figure.
- Logic App Consumption: one recurrence trigger and one built-in HTTP action per day, so about 30 built-in actions per
  month against the free tier of 4,000 (meter logicapp_consumption_builtin_action_free_tier). The paid built-in price is
  rounded to 0 by the retail API (precision_limited), so it is shown as UNPRICED beyond the free tier; this stays 0.
- Not priced (no meter in prices.yaml or API precision too low): outbound data transfer (a few KiB CSV and MiB of
  Parquet read inside the region), boot diagnostics (managed storage), Log Analytics ingestion (no agent installed),
  NAT gateway (rejected alternative, no meter fetched).
"""

import sys
from pathlib import Path

import yaml

HOURS = 730
RUNS = 30
RUN_MINUTES = 10

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
with path.open(encoding="utf-8") as fh:
    P = {p["key"]: p["unitPrice"] for p in yaml.safe_load(fh)["prices"]}

B1S = P["vm_b1s_hour"]
B2S = P["vm_b2s_hour"]
DISK = P["disk_standard_ssd_e4_month"]
PIP = P["publicip_standard_static_hour"]

run_hours = RUNS * RUN_MINUTES / 60
active_vm = B1S * run_hours
always_on_vm = B1S * HOURS

print("| Component | Quantity x unit price (EUR) | Scheduled EUR/month | Parked EUR/month |")
print("|---|---|---:|---:|")
print(
    f"| VM Standard_B1s compute | {run_hours:.1f} h ({RUNS} x {RUN_MINUTES} min) x {B1S} | {active_vm:.2f} | 0.00 |"
)
print(
    f"| OS disk StandardSSD E4 (billed while deallocated) | 1 x {DISK} | {DISK:.2f} | {DISK:.2f} deallocated, 0.00 if enable_legacy_vm = false |"
)
print(f"| Logic App Consumption | {RUNS} built-in actions, free tier 4,000 | 0.00 | 0.00 |")
print(f"| **Priced subtotal** | | **{active_vm + DISK:.2f}** | **0.00 to {DISK:.2f}** |")
print()
print("Variants (not chosen):")
print()
print("| Variant | EUR/month | Delta vs chosen |")
print("|---|---:|---:|")
print(
    f"| B1s left running 24/7 (exporter failed to deallocate, 20:00 auto-shutdown is the net) | {always_on_vm + DISK:.2f} | +{always_on_vm - active_vm:.2f} |"
)
print(
    f"| B2s scheduled instead of B1s | {B2S * run_hours + DISK:.2f} | +{(B2S - B1S) * run_hours:.2f} |"
)
print(
    f"| Standard static public IP for outbound instead of default outbound access | {active_vm + DISK + PIP * HOURS:.2f} | +{PIP * HOURS:.2f} |"
)
