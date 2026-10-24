# ruff: noqa: T201, E501  # standalone report script: prints the c4-c10.md model tables
"""Deterministic MODELLED EUR for SPEC-09 rows C4-C10 (T-504). CLAUDE.md 2.2: no number is typed in except SCENARIOS.

    python -I docs/finops/results/c4_c10_model.py [docs/finops/prices.yaml]

Unit prices come from prices.yaml (westeurope, EUR); a month is 730 h. SCENARIOS are stated assumptions, each
replaced by a MEASURED value when the cloud window runs (see the measurement method in c4-c10.md). No randomness, no network.
"""

import sys
from pathlib import Path

import yaml

HOURS = 730
path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
with path.open(encoding="utf-8") as fh:
    P = {p["key"]: p["unitPrice"] for p in yaml.safe_load(fh)["prices"]}

# --- SCENARIOS (assumptions) -------------------------------------------------------------------
SPOT_NODE_HOURS = {"light": 40, "heavy": 150}  # T-306 delta scenarios
BASELINE_NODES = 5  # SPEC-09 C4 baseline: 5x D4as_v5 on demand 24/7
SQL_ACTIVE_H = 40  # SPEC-07 active month
SQL_VCORES_ACTIVE = {"0.5 vCore": 0.5, "2 vCore": 2.0}
SQL_STORAGE_GB = 32
VM_RUNS, VM_RUN_MIN = 30, 10  # T-310
LOG_BASELINE_GB_DAY = {"0.5 GB/day": 0.5, "1 GB/day": 1.0}  # uncapped diagnostics, assumption
LOG_CAP_GB_DAY = 0.2  # T-120
LOG_FREE_GB = 5
ACR_IMAGE_GB, ACR_MERGES_MONTH, ACR_INCLUDED_GB = (
    0.25,
    40,
    10,
)  # compressed image size and CI cadence, assumptions
PARK_WORK_HOURS_MONTH = 50 * 52 / 12  # 50 h/week, "make park" outside work hours

print("## C4 spot autoscaling vs fixed on-demand (compute + prorated OS disk, MODELLED)\n")
od, spot, p4 = P["vm_d4as_v5_hour"], P["vm_d4as_v5_spot_hour"], P["disk_premium_p4_month"]
base = BASELINE_NODES * HOURS * od
print(
    f"- Baseline: {BASELINE_NODES} x D4as_v5 on demand x {HOURS} h x {od} = **{base:.2f} EUR/month** (compute only; idle share = 100 % of hours without a job)"
)
print(
    "\n| Scenario | Spot node-hours | Spot EUR/month (compute + 32 GB P4 disk) | Same hours on demand | Saving vs 24/7 baseline | Idle EUR (nodes at 0) |"
)
print("|---|---:|---:|---:|---:|---:|")
for k, h in SPOT_NODE_HOURS.items():
    s = h * (spot + p4 / HOURS)
    print(
        f"| {k} | {h} | {s:.2f} | {h * od:.2f} | {base - s:.2f} ({(1 - s / base) * 100:.1f} %) | 0.00 |"
    )
print(
    f"\nSpot price ratio spot/on-demand: {spot / od:.3f}. EUR per 100 backtests = node-hours per 100 backtests x {spot + p4 / HOURS:.4f}; node-hours per 100 backtests is NOT modelled (NEEDS-CLOUD)."
)

print("\n## C5 Container Apps (min 0) vs always-on App Service B2\n")
b2, b1 = P["appservice_b2_linux_hour"], P["appservice_b1_linux_hour"]
print(f"- Baseline always-on B2: {HOURS} h x {b2} = **{b2 * HOURS:.2f} EUR/month**")
print(
    "- Optimised: 90,000 vCPU-s and 180,000 GiB-s typical month (T-309 model) = 50 % of the free grants (180,000 / 360,000): **0.00 EUR priced** (compute meters round to 0.0 in the retail API, so the real figure is MEASURED from the cost export)"
)
print(
    f"- Saving: {b2 * HOURS:.2f} EUR/month (B1 for the API plan, kept either way: {b1 * HOURS:.2f})"
)

print("\n## C6 SQL serverless auto-pause vs provisioned GP 2 vCore\n")
pv, sv, st = (
    P["sql_gp_provisioned_vcore_hour"],
    P["sql_gp_serverless_vcore_hour"],
    P["sql_gp_storage_gb_month"],
)
stor = SQL_STORAGE_GB * st
prov = 2 * HOURS * pv + stor
print(
    f"- Baseline provisioned 2 vCore: 2 x {HOURS} h x {pv} + {SQL_STORAGE_GB} GB x {st} = **{prov:.2f} EUR/month** (lower bound, see prices.yaml note on licence)"
)
print("\n| Scenario | vCore-seconds | Compute EUR | Storage EUR | Total EUR/month | Saving |")
print("|---|---:|---:|---:|---:|---:|")
for k, v in SQL_VCORES_ACTIVE.items():
    vs = v * SQL_ACTIVE_H * 3600
    c = vs / 3600 * sv
    print(
        f"| {k} for {SQL_ACTIVE_H} h, paused otherwise | {vs:,.0f} | {c:.2f} | {stor:.2f} | {c + stor:.2f} | {prov - c - stor:.2f} |"
    )
print(f"| Paused all month | 0 | 0.00 | {stor:.2f} | {stor:.2f} | {prov - stor:.2f} |")

print("\n## C7 scheduled VM vs B1s 24/7\n")
b1s, e4 = P["vm_b1s_hour"], P["disk_standard_ssd_e4_month"]
run_h = VM_RUNS * VM_RUN_MIN / 60
print(f"- Baseline 24/7: {HOURS} h x {b1s} + disk {e4} = **{b1s * HOURS + e4:.2f} EUR/month**")
print(
    f"- Optimised: {run_h:.1f} h x {b1s} + disk {e4} = **{b1s * run_h + e4:.2f} EUR/month** (saving {b1s * (HOURS - run_h):.2f}; disk is billed while deallocated)"
)

print("\n## C8 log ingestion cap\n")
ing, free = P["loganalytics_ingestion_gb"], LOG_FREE_GB
bas = P["loganalytics_basic_ingestion_gb"]


def la(gb_month: float) -> float:
    """Analytics Logs cost: first 5 GB free, rest at the paid rate."""
    return max(0.0, gb_month - free) * ing


capped = LOG_CAP_GB_DAY * 30
print(
    f"- Optimised ceiling: {LOG_CAP_GB_DAY} GB/day x 30 = {capped:.1f} GB/month -> **{la(capped):.2f} EUR/month**"
)
print("\n| Uncapped baseline (assumed) | GB/month | EUR/month | Saving vs capped ceiling |")
print("|---|---:|---:|---:|")
for k, g in LOG_BASELINE_GB_DAY.items():
    print(f"| {k} | {g * 30:.1f} | {la(g * 30):.2f} | {la(g * 30) - la(capped):.2f} |")
print(
    f"\nBasic Logs table: {bas} EUR/GB with no free tier, so moving the {capped:.1f} GB ceiling there costs {capped * bas:.2f} EUR (worse than the free-tier Analytics path); Basic Logs pays only for high-volume tables beyond the free 5 GB."
)

print("\n## C9 ACR purge\n")
sg = P["acr_basic_storage_gb_month"]
print(
    f"- Assumption: {ACR_MERGES_MONTH} images/month x {ACR_IMAGE_GB} GB, {ACR_INCLUDED_GB} GB included in the Basic unit, {sg} EUR/GB-month above it"
)
print(
    "\n| Month since start | Stored GB without purge | EUR/month without purge | With weekly purge (capped <= included) |"
)
print("|---|---:|---:|---:|")
for m in (1, 3, 6, 12):
    gb = m * ACR_MERGES_MONTH * ACR_IMAGE_GB
    print(f"| {m} | {gb:.0f} | {max(0, gb - ACR_INCLUDED_GB) * sg:.2f} | 0.00 |")

print("\n## C10 parking (make park) vs always on\n")
b2s = P["vm_b2s_hour"]
lb, pip = P["lb_standard_rules_hour"], P["publicip_standard_static_hour"]
always = b2s * HOURS + b1 * HOURS + p4 + lb * HOURS + pip * HOURS
parked = (
    p4 + lb * HOURS + pip * HOURS
)  # LB/IP/disk retained when parked (UNVERIFIED, T-306 finding)
whrs = PARK_WORK_HOURS_MONTH
partial = b2s * whrs + b1 * whrs + p4 + lb * HOURS + pip * HOURS
print(
    f"- Stoppable lines: AKS system B2s ({b2s}/h) and App Service B1 ({b1}/h, plan deleted when parked); retained: P4 disk {p4}, LB {lb * HOURS:.2f}, IP {pip * HOURS:.2f}"
)
print(
    f"- Always on: **{always:.2f} EUR/month**; parked outside work hours ({whrs:.0f} h/month of 730): **{partial:.2f}**; fully parked month: **{parked:.2f}**"
)
print(
    f"- Saving: {always - partial:.2f} (work-hours only), {always - parked:.2f} (whole month). ACR, storage, SQL not parked (identical both cases, excluded)"
)
