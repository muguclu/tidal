# ruff: noqa: T201, E501  # standalone report script: prints the delta tables
"""Deterministic cost model behind docs/finops/deltas/T-407.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-407_model.py [docs/finops/prices.yaml]

Unit prices are read from prices.yaml only (including defender_sql_server_month). Event counts and sizes are ASSUMPTIONS, not measurements; replace them with a Log Analytics query
(`AzureDiagnostics | where Category == "SQLSecurityAuditEvents" | summarize sum(_BilledSize)`) after the first week.

Assumptions (stated, not measured):
- One audit record in AzureDiagnostics is about 2 KiB billed (it is a wide table; many columns are populated per event).
- Dev traffic: the API and worker open pooled connections; with auto-pause a connection pool restarts after each resume.
  SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP: 500 events/day. BATCH_COMPLETED_GROUP: 20,000 batches/day on an active day.
  FAILED_DATABASE_AUTHENTICATION_GROUP and the change groups: 50 events/day combined (token expiry, scanner noise).
- The workspace cap (modules/log_analytics, SPEC-07) is 0.2 GB/day and is shared with App Insights and every other source.
- Defender for SQL (Azure SQL Database servers plan) is billed per logical server per month, prorated per day,
  independent of database auto-pause or the free offer. Meter: Microsoft Defender for SQL, SKU Standard,
  meterName "Standard Node", unit 1/Month, key defender_sql_server_month in prices.yaml.
"""

import sys
from pathlib import Path

import yaml

KIB = 1024
GIB = 1024**3
RECORD_BYTES = 2 * KIB
CAP_GB_DAY = 0.2
MONTH_DAYS = 30

# Each scenario is a name and an events-per-day figure.
SCENARIOS = (
    ("A dev default: failed logins + change groups", 50),
    ("B A + successful authentication", 50 + 500),
    ("C B + BATCH_COMPLETED_GROUP (active day)", 50 + 500 + 20_000),
)

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
with path.open(encoding="utf-8") as fh:
    P = {p["key"]: p["unitPrice"] for p in yaml.safe_load(fh)["prices"]}

ING = P["loganalytics_ingestion_gb"]  # above the 5 GB/month free tier
FREE_GB = 5.0
defender = P["defender_sql_server_month"]

print(
    "| Scenario | Events/day | GB/day | % of 0.2 GB/day cap | GB/month | Ingestion EUR/month (5 GB free, rest at list) |"
)
print("|---|---:|---:|---:|---:|---:|")
for name, events in SCENARIOS:
    gb_day = events * RECORD_BYTES / GIB
    gb_month = gb_day * MONTH_DAYS
    eur = max(0.0, gb_month - FREE_GB) * ING
    print(
        f"| {name} | {events:,} | {gb_day:.4f} | {100 * gb_day / CAP_GB_DAY:.1f} | {gb_month:.3f} | {eur:.2f} |"
    )

print()
print(
    f"Log Analytics paid ingestion {ING} EUR/GB; the 0.2 GB/day cap bounds ingestion at {CAP_GB_DAY * MONTH_DAYS:.1f} GB/month "
    f"= {max(0.0, CAP_GB_DAY * MONTH_DAYS - FREE_GB) * ING:.2f} EUR/month for ALL sources together (already in the cost model as the ceiling)."
)
print()
print("| Defender option | EUR/month | Note |")
print("|---|---:|---|")
print(
    f"| Defender for SQL on the server, 30 days | {defender:.2f} | per server, not reduced by auto-pause or the free offer |"
)
print(
    f"| Defender for SQL, 1 demo day per month | {defender / MONTH_DAYS:.2f} | prorated per day (assumption) |"
)
print(
    "| Vulnerability Assessment, Express configuration | 0.00 | no separate meter; requires Defender on the server |"
)
print("| Off (decision) | 0.00 | |")
print()
print(
    f"Defender 30 days vs whole dev subtotal ceiling 7.33 (cost-model): +{100 * defender / 7.33:.0f} %"
)
