# ruff: noqa: T201, E501  # standalone report script: prints the delta table
"""Deterministic cost model behind docs/finops/deltas/T-506.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-506_model.py [docs/finops/prices.yaml]

Unit prices come from prices.yaml. Volumes are stated assumptions, not measurements (NEEDS-CLOUD: replace with the Usage table
after one week). Activity-log alerts, the workbook, Analytics-log queries and action-group emails have no meter.

Assumptions:
- Log alert rules bill per rule per evaluation-frequency tier: 5 min for the API 5xx rule, the 15 min (or slower) tier for
  the hourly freshness rule and the 15 min rules. Dev has Container Insights off, so the spot eviction rule is not created.
- Metric alert: one time series per Service Bus queue (3, 4 with the ingest topic), inside the 10 free series.
- Blob audit: StorageWrite + StorageDelete records for the whole account (the diagnostic setting cannot filter operations):
  OPS_PER_DAY records of RECORD_BYTES each. The workspace is capped at 0.2 GB/day; the audit share is shown against that cap.
- Workbook metric tiles: VIEWS_PER_MONTH views x METRIC_TILES native metric calls (first 1,000 calls/month are free).
"""

import sys
from pathlib import Path

import yaml

OPS_PER_DAY = {"low": 2_000, "high": 20_000}
RECORD_BYTES = 1_200
DAYS = 30
CAP_GB_PER_DAY = 0.2
METRIC_SERIES = 4
VIEWS_PER_MONTH = 60
METRIC_TILES = 4


def main() -> None:
    """Print the T-506 delta tables from prices.yaml."""
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
    p = {r["key"]: r for r in yaml.safe_load(path.read_text(encoding="utf-8"))["prices"]}
    r5 = p["alerts_log_rule_5min_month"]["unitPrice"]
    r15 = p["alerts_log_rule_15min_month"]["unitPrice"]
    series = p["alerts_metric_series_month"]["unitPrice"]
    ingest = p["loganalytics_ingestion_gb"]["unitPrice"]
    print(
        f"prices: 5 min rule {r5}, 15 min rule {r15}, metric series >10 {series}, LA ingestion above free tier {ingest} /GB"
    )
    print()
    rules = [
        ("API 5xx (log, 5 min)", r5),
        ("Ingest freshness (log, hourly = 15 min tier)", r15),
        ("LA daily cap (log, 15 min)", r15),
        ("Blob audit (log, 15 min)", r15),
        ("Spot eviction (log, 15 min), NOT created in dev", 0.0),
        ("DLQ (metric, series inside 10 free)", series * max(METRIC_SERIES - 10, 0)),
        ("Activity-log alerts x3 (mgmt policy, role write, role delete)", 0.0),
    ]
    print("| Alert | EUR/month |")
    print("|---|---|")
    for name, c in rules:
        print(f"| {name} | {c:.4f} |")
    alerts = sum(c for _, c in rules)
    print(f"| **Alert rules total** | **{alerts:.4f}** |")
    print()
    calls = VIEWS_PER_MONTH * METRIC_TILES
    print(
        f"workbook metric calls/month: {calls} (free up to 1000) -> 0.0000 EUR; KQL tiles on Analytics logs: no meter"
    )
    print()
    print(
        "| Blob audit volume | GB/month | % of daily cap | EUR/month if billed (above 5 GB free) | EUR/month if inside the free 5 GB |"
    )
    print("|---|---|---|---|---|")
    for name, ops in OPS_PER_DAY.items():
        gb_day = ops * RECORD_BYTES / 1e9
        gb_month = gb_day * DAYS
        print(
            f"| {name}: {ops} ops/day | {gb_month:.3f} | {100 * gb_day / CAP_GB_PER_DAY:.1f} | {gb_month * ingest:.4f} | 0.0000 |"
        )
    print()
    for name, ops in OPS_PER_DAY.items():
        gb_month = ops * RECORD_BYTES / 1e9 * DAYS
        print(
            f"total delta ({name}): {alerts + gb_month * ingest:.4f} EUR/month (audit ingestion billed)"
        )


if __name__ == "__main__":
    main()
