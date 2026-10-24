# ruff: noqa: T201, E501  # standalone report script: prints the delta table
"""Deterministic cost model behind docs/finops/deltas/T-502.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-502_model.py [docs/finops/prices.yaml]

Unit prices come from prices.yaml. Sizes come from the committed fixture tests/fixtures/finops/actual-mtd-2026-10-24.csv
(bytes per row, rows per day: MEASURED on synthetic data, so the real figure is NEEDS-CLOUD). The cost export itself
has no meter (Cost Management exports are free), so the delta is Hot storage + blob transactions only.

Assumptions (stated, not measured):
- 12 month folders are kept (no lifecycle rule); each holds the latest overwritten run (one CSV partition + manifest.json).
- Azure writes 2 blobs per run, 1 run per day (2 on the first 5 days of a month: ignored, <1 % of writes).
- One weekly workflow run lists and downloads every blob (LIST is billed as a write-class operation, GET as read): LISTS_PER_RUN
  and the 12 CSV + 12 manifest blobs read once each.
- A month is 30 days for the export size and 730 h elsewhere (prices.yaml convention).
"""

import csv
import sys
from pathlib import Path

import yaml

FIXTURE = Path("tests/fixtures/finops/actual-mtd-2026-10-24.csv")
MONTHS_KEPT = 12
DAYS_PER_MONTH = 30
BLOBS_PER_RUN = 2
RUNS_PER_MONTH_DAYS = DAYS_PER_MONTH
WORKFLOW_RUNS_PER_MONTH = 4.345
LISTS_PER_RUN = 2
BLOBS_READ_PER_RUN = MONTHS_KEPT * 2
SCENARIOS = {"fixture rows/day": 1.0, "10x rows/day (AKS, more meters)": 10.0}


def main() -> None:
    """Print the T-502 delta tables from prices.yaml."""
    prices_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
    prices = {
        p["key"]: p for p in yaml.safe_load(prices_path.read_text(encoding="utf-8"))["prices"]
    }
    stored = prices["blob_hot_stored_gb_month"]["unitPrice"]
    write_10k = prices["blob_hot_write_10k"]["unitPrice"]
    read_10k = prices["blob_hot_read_10k"]["unitPrice"]

    data = FIXTURE.read_bytes()
    with FIXTURE.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    days = len({r["Date"] for r in rows})
    bytes_per_row = len(data) / len(rows)
    rows_per_day = len(rows) / days
    print(
        f"fixture (MEASURED, synthetic): {len(rows)} rows, {days} days, {bytes_per_row:.1f} B/row, {rows_per_day:.1f} rows/day"
    )
    print(
        f"prices (prices.yaml): stored {stored} /GB-month, write {write_10k} /10K, read {read_10k} /10K"
    )
    print()
    print(
        "| Scenario | MB per month file | GB stored (12 months) | Stored | Writes | Reads | Total EUR/month |"
    )
    print("|---|---|---|---|---|---|---|")
    for name, factor in SCENARIOS.items():
        month_bytes = rows_per_day * factor * bytes_per_row * DAYS_PER_MONTH
        gb_stored = month_bytes * MONTHS_KEPT / 1e9
        c_stored = gb_stored * stored
        writes = RUNS_PER_MONTH_DAYS * BLOBS_PER_RUN + WORKFLOW_RUNS_PER_MONTH * LISTS_PER_RUN
        reads = WORKFLOW_RUNS_PER_MONTH * BLOBS_READ_PER_RUN
        c_writes = writes / 10_000 * write_10k
        c_reads = reads / 10_000 * read_10k
        total = c_stored + c_writes + c_reads
        print(
            f"| {name} | {month_bytes / 1e6:.3f} | {gb_stored:.6f} | {c_stored:.6f} | {c_writes:.6f} | {c_reads:.6f} | {total:.6f} |"
        )


if __name__ == "__main__":
    main()
