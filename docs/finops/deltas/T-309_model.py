# ruff: noqa: T201, E501  # standalone report script: prints the delta tables
"""Deterministic cost model behind docs/finops/deltas/T-309.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-309_model.py [docs/finops/prices.yaml]

Unit prices are read from prices.yaml; nothing is typed in except the usage SCENARIOS below.

Assumptions (stated, not measured):
- A month is 730 hours (the convention in the prices.yaml notes).
- App Service: one B1 Linux plan, always on, 730 h. The web app itself has no extra meter. Deleting it
  (enable_app_service = false) removes the plan, so the parked cost is 0.
- Container Apps compute is NOT priced in EUR here. Decision C5 (src/tidal/finops/fetch_prices.py): the per-second
  meters come back as 0.0 from the retail API (4-decimal rounding), so Container Apps compute is MEASURED from the
  Cost Management export, never MODELLED from these meters. What this script does instead is size the monthly usage
  against the published free grants (180,000 vCPU-s, 360,000 GiB-s, 2M requests per month and subscription); the
  grants are not in the API and come from the Azure price list, stated in the delta. Anything above a grant is
  listed as UNPRICED, with the meter that would apply.
- Usage scenarios (run time per execution is an assumption, replaced by C5 measurements):
  query app 0.5 vCPU / 1 GiB, 40 active hours per month (SPEC-07 "active month"), idle replicas = 0 so no idle meter;
  synth-daily 1 vCPU / 2 GiB, 600 s per day, 30 days; the tiering event job (1 vCPU / 2 GiB, 600 s, 20 runs) exists only when
  enable_tiering_event_job is true (default false) and is shown as a separate scenario;
  tiering-yearly 2 vCPU / 4 GiB at its 6 h timeout (worst case), once a year, shown for the month it runs (January).
- Not priced (no meter in prices.yaml or API precision too low): Container Apps vCPU/GiB-seconds, the Container Apps
  environment (consumption: no base charge), outbound data transfer, requests above the 2M grant (meter exists,
  containerapps_requests_1m, but the query app is nowhere near it).
"""

import sys
from pathlib import Path

import yaml

HOURS = 730
path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
with path.open(encoding="utf-8") as fh:
    P = {p["key"]: p["unitPrice"] for p in yaml.safe_load(fh)["prices"]}

B1 = P["appservice_b1_linux_hour"]
B2 = P["appservice_b2_linux_hour"]
REQ = P["containerapps_requests_1m"]

GRANT_VCPU_S, GRANT_GIB_S, GRANT_REQ_M = 180_000, 360_000, 2.0

print("| Component | Quantity x unit price (EUR) | Active EUR/month | Parked EUR/month |")
print("|---|---|---:|---:|")
print(
    f"| App Service plan B1 Linux (tidal-api) | {HOURS} h x {B1} | {B1 * HOURS:.2f} | 0.00 (enable_app_service = false) or {B1 * HOURS:.2f} if kept |"
)
print("| Container Apps environment (consumption) | no base charge | 0.00 | 0.00 |")
print(
    "| Container Apps compute (query + 2 jobs, +1 with the tiering flag) | see usage table; MEASURED in C5 | UNPRICED (within free grant) | 0.00 |"
)
print(f"| **Priced subtotal** | | **{B1 * HOURS:.2f}** | **0.00 to {B1 * HOURS:.2f}** |")
print()
print(
    f"Variant: App Service B2 instead of B1 would cost {B2 * HOURS:.2f} EUR/month (+{(B2 - B1) * HOURS:.2f}); B1 is kept (SPEC-07 section 2)."
)
print()

# name, vCPU, GiB, seconds per execution, executions per month
USAGE = [
    ("tidal-query (40 active h, min replicas 0)", 0.5, 1.0, 40 * 3600, 1),
    ("synth-daily (600 s x 30)", 1.0, 2.0, 600, 30),
    # Only with enable_tiering_event_job = true (default false, T-403); see the scenario table.
]
TIERING = ("tiering event job, flag on (600 s x 20 messages)", 1.0, 2.0, 600, 20)
YEARLY = ("tiering-yearly (6 h timeout, January only)", 2.0, 4.0, 6 * 3600, 1)

print("| Workload | vCPU | GiB | vCPU-s/month | GiB-s/month |")
print("|---|---:|---:|---:|---:|")
tv = tg = 0.0
for name, c, g, s, n in USAGE:
    print(f"| {name} | {c} | {g} | {c * s * n:,.0f} | {g * s * n:,.0f} |")
    tv += c * s * n
    tg += g * s * n
name, c, g, s, n = YEARLY
yv, yg = c * s * n, g * s * n
print(f"| {name} | {c} | {g} | {yv:,.0f} | {yg:,.0f} |")
_, tc, tgb, ts, tn = TIERING
xv, xg = tc * ts * tn, tgb * ts * tn
print(f"| {TIERING[0]} | {tc} | {tgb} | {xv:,.0f} | {xg:,.0f} |")
print()
print("| Month | vCPU-s | vs grant 180,000 | GiB-s | vs grant 360,000 | Above grant |")
print("|---|---:|---:|---:|---:|---|")
for label, v, g_ in (
    ("typical (default flags)", tv, tg),
    ("January (yearly job runs)", tv + yv, tg + yg),
    ("typical + tiering event job", tv + xv, tg + xg),
    ("January + tiering event job", tv + yv + xv, tg + yg + xg),
):
    over = (
        "none"
        if v <= GRANT_VCPU_S and g_ <= GRANT_GIB_S
        else "UNPRICED (meter has no usable price, C5)"
    )
    print(
        f"| {label} | {v:,.0f} | {v / GRANT_VCPU_S:.0%} | {g_:,.0f} | {g_ / GRANT_GIB_S:.0%} | {over} |"
    )
print()
print(
    f"Requests: grant {GRANT_REQ_M:.0f}M/month; above it {REQ} EUR per 1M (meter containerapps_requests_1m)."
)
