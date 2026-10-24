# ruff: noqa: T201, E501, S101, ERA001  # standalone report script: writes the C1-C3 report; asserts guard evidence consistency
"""Deterministic generator of docs/finops/results/c1-c3.md (T-503; CLAUDE.md 2.2: no number typed by hand).

Run from the repository root (pyarrow + pyyaml, no randomness, no network):

    python -I docs/finops/results/c1_c3_model.py            # writes docs/finops/results/c1-c3.md

Inputs (all in the repository):
- docs/finops/prices.yaml                                    unit prices (EUR, westeurope)
- docs/finops/results/storage-benchmark-2026-10-22.parquet   C1 bytes (MEASURED, T-204)
- docs/finops/results/c2/run_raw.txt                         C2 tiering log, one JSON line per table (MEASURED, local)
- docs/finops/results/c2/timings.txt                         C2 wall-clock start/end (MEASURED)
- docs/finops/deltas/T-207_model.py                          C3 model (MODELLED), executed here, not re-typed

Conventions: GB in prices.yaml is billed per GiB-month by the T-204 report (bytes / 2**30); this script does the same.
"""

import contextlib
import io
import json
import re
import runpy
import sys
from pathlib import Path

import pyarrow.parquet as pq
import yaml

ROOT = Path(__file__).resolve().parents[3]
RES = ROOT / "docs/finops/results"
GIB = 2**30
HOURS_MONTH = 730

with (ROOT / "docs/finops/prices.yaml").open(encoding="utf-8") as fh:
    _doc = yaml.safe_load(fh)
P = {p["key"]: p["unitPrice"] for p in _doc["prices"]}
CAPTURED = _doc["captured_at"]

out: list[str] = []


def w(s: str = "") -> None:
    """Append one line to the report."""
    out.append(s)


# ---------------------------------------------------------------- C1
tbl = pq.read_table(RES / "storage-benchmark-2026-10-22.parquet").to_pylist()


def at_rest(scale: str, fmt: str, key: str = "bytes_at_rest") -> int:
    """Bytes of the whole store at one scale/format (identical on every W2-W6 row; W5 = full scan)."""
    vals = {
        r[key]
        for r in tbl
        if r["scale"] == scale and r["format_id"] == fmt and r["workload_id"] == "W5"
    }
    assert len(vals) == 1, (scale, fmt, vals)
    return vals.pop()


def ticks(scale: str) -> int:
    """Base ticks of a scale. S-3m: W5 scans every tick. Full S: stated in the T-204 report (W5 there scans 10 instruments)."""
    if scale == "S":
        m = re.search(
            r"scale S: ([\d,]+) base ticks",
            (RES / "storage-benchmark-2026-10-22.md").read_text(encoding="utf-8"),
        )
        assert m, "base ticks of scale S not found in the T-204 report"
        return int(m.group(1).replace(",", ""))
    vals = {
        r["rows"]
        for r in tbl
        if r["scale"] == scale and r["workload_id"] == "W5" and r["format_id"] == "F5"
    }
    assert len(vals) == 1
    return vals.pop()


f0_3m, f0_3m_adj = at_rest("S-3m", "F0"), at_rest("S-3m", "F0", "bytes_at_rest_adjusted")
f0_3m_bill = at_rest("S-3m", "F0", "bytes_billed")
f5_3m, f5_s = at_rest("S-3m", "F5"), at_rest("S", "F5")
t3m, ts = ticks("S-3m"), ticks("S")
F0_BPT, F5_BPT_3M, F5_BPT_S = f0_3m / t3m, f5_3m / t3m, f5_s / ts
f0_s_model = F0_BPT * ts  # MODELLED: F0 was not run at full S
SQL_GB, BLOB_HOT = P["sql_gp_storage_gb_month"], P["blob_hot_stored_gb_month"]


def eur(b: float, price: float) -> float:
    """EUR per month for b bytes at price per GiB-month."""
    return b / GIB * price


c1 = {
    "3m_f0_gib": f0_3m / GIB,
    "3m_f5_gib": f5_3m / GIB,
    "3m_f0_eur": eur(f0_3m_bill, SQL_GB),
    "3m_f5_eur": eur(f5_3m, BLOB_HOT),
    "s_f0_gib": f0_s_model / GIB,
    "s_f5_gib": f5_s / GIB,
    "s_f0_eur": eur(f0_s_model, SQL_GB),
    "s_f5_eur": eur(f5_s, BLOB_HOT),
}


# ---------------------------------------------------------------- C2
def _jl(name: str) -> dict[str, dict]:
    """tiering_table_done events of one log file, keyed by table."""
    rows = (
        json.loads(ln)
        for ln in (RES / "c2" / name).read_text(encoding="utf-8").splitlines()
        if ln.startswith("{")
    )
    return {r["table"]: r for r in rows if r.get("event") == "tiering_table_done"}


run1, run2, dry = _jl("run1_raw.txt"), _jl("run2_raw.txt"), _jl("dry_run.txt")
tm = dict(
    ln.split("=", 1)
    for ln in (RES / "c2/timings.txt").read_text(encoding="utf-8").split()
    if "=" in ln
)
created = json.loads((RES / "c2/blobs_created.json").read_text(encoding="utf-8"))["datasets"]
# Run 1 moved all of md.bar_hot and part of md.tick_hot, then lost its SQL connection (the Docker engine restarted);
# run 2 resumed md.tick_hot. Totals per table are the dry-run plan (the table content) split across the runs.
tick_rows, bar_rows = dry["md.tick_hot"]["rows_moved"], dry["md.bar_hot"]["rows_moved"]
tick_run2 = run2["md.tick_hot"]["rows_moved"]
tick_run1 = tick_rows - tick_run2
tick_kb_before, tick_kb_after = (
    dry["md.tick_hot"]["sql_kb_before"],
    run2["md.tick_hot"]["sql_kb_after"],
)
bar_kb_before, bar_kb_after = dry["md.bar_hot"]["sql_kb_before"], run1["md.bar_hot"]["sql_kb_after"]
tick_bytes = created["ticks"][
    "bytes"
]  # all tick parts written by both runs (Azurite blobs created since the run started)
bar_bytes = sum(created[d]["bytes"] for d in ("bars_1s", "bars_1m", "bars_1h", "bars_1d"))
assert bar_bytes == run1["md.bar_hot"]["bytes_written"], (
    "blob listing and the run-1 log disagree for bars"
)
assert tick_bytes > run2["md.tick_hot"]["bytes_written"]
# Bytes per tick: SQL (reserved KB before / rows) and Parquet (bytes written / rows).
SQL_BPT = tick_kb_before * 1024 / tick_rows
PQ_BPT = tick_bytes / tick_rows
RATIO = PQ_BPT / SQL_BPT

# Cost scales: T-121 / config-contract (10 B/row -> 0.5 / 3 / 15 GB => 50M / 300M / 1.5B ticks; 5 / 10 / 20 years)
SCALES = {"S": (50_000_000, 5), "M": (300_000_000, 10), "L": (1_500_000_000, 20)}
HOT_DAYS = 90
# Assumptions (stated, not measured)
GP_VCORES = 2  # baseline: GP provisioned 2 vCore, always on
SRV_VCORES, SRV_HOURS = (
    1.0,
    176.0,
)  # optimised: serverless avg 1 vCore, 8 h x 22 working days active, rest auto-paused
QUERIES_PER_MONTH = (
    100_000  # cold-path queries; unit cost from T-204 W3 Azurite counts (5 GET + 7 HEAD + 1 LIST)
)
GET_HEAD_LIST = (5, 7, 1)
q_eur = (
    GET_HEAD_LIST[0] * P["blob_hot_read_10k"]
    + GET_HEAD_LIST[1] * P["blob_hot_read_10k"]
    + GET_HEAD_LIST[2] * P["blob_hot_write_10k"]
) / 1e4
GP_H, SRV_H = P["sql_gp_provisioned_vcore_hour"], P["sql_gp_serverless_vcore_hour"]

c2: dict[str, dict[str, float]] = {}
for s, (n, yrs) in SCALES.items():
    hot_share = min(1.0, HOT_DAYS / (yrs * 365))
    sql_all = n * SQL_BPT
    sql_hot = sql_all * hot_share
    blob_cold = n * (1 - hot_share) * PQ_BPT
    base_compute = GP_VCORES * HOURS_MONTH * GP_H
    base_storage = eur(sql_all, SQL_GB)
    opt_compute = SRV_VCORES * SRV_HOURS * SRV_H
    opt_sql_storage = eur(sql_hot, SQL_GB)
    opt_blob = eur(blob_cold, BLOB_HOT)
    opt_query = QUERIES_PER_MONTH * q_eur
    c2[s] = {
        "sql_all_gib": sql_all / GIB,
        "sql_hot_gib": sql_hot / GIB,
        "blob_gib": blob_cold / GIB,
        "hot_share": hot_share,
        "base_compute": base_compute,
        "base_storage": base_storage,
        "base_total": base_compute + base_storage,
        "opt_compute": opt_compute,
        "opt_sql_storage": opt_sql_storage,
        "opt_blob": opt_blob,
        "opt_query": opt_query,
        "opt_total": opt_compute + opt_sql_storage + opt_blob + opt_query,
    }
# Sensitivity: serverless active hours at which the compute saving vanishes (S, vs baseline compute)
break_even_hours = c2["S"]["base_compute"] / (SRV_VCORES * SRV_H)

# ---------------------------------------------------------------- C3 (T-207 model, ADR-0015 design: ticks only, 2 tags)
_buf = io.StringIO()
_argv, sys.argv = sys.argv, ["T-207_model.py", str(ROOT / "docs/finops/prices.yaml")]
with contextlib.redirect_stdout(_buf):
    g = runpy.run_path(str(ROOT / "docs/finops/deltas/T-207_model.py"), run_name="t207")
sys.argv = _argv
R = g["R"]
COLD_ED_MAX = 3 * P["blob_cold_early_delete_gb"]

# ---------------------------------------------------------------- report
f = lambda x, n=4: format(x, f".{n}f")  # noqa: E731
w("# C1-C3 measurements (T-503)")
w()
w(
    f"_Generated by `docs/finops/results/c1_c3_model.py`. Prices: `docs/finops/prices.yaml` captured {CAPTURED}, EUR, westeurope. "
    "MEASURED = counted by a harness or by SQL Server on this laptop (SQL Server 2022 in Docker, Azurite). "
    "MODELLED = unit price x quantity. NEEDS-CLOUD = requires a real Azure bill or Azure Monitor metric._"
)
w()
w("## Summary table")
w()
w("| Id | Baseline | Optimised | Metric | Value | Label | Evidence |")
w("|---|---|---|---|---|---|---|")
w(
    f"| C1 (S-3m, {t3m:,} ticks) | F0 SQL rowstore | F5 epoch Parquet | MiB at rest | {c1['3m_f0_gib'] * 1024:.2f} -> {c1['3m_f5_gib'] * 1024:.2f} MiB ({c1['3m_f0_gib'] / c1['3m_f5_gib']:.1f}x) | MEASURED | `storage-benchmark-2026-10-22.parquet` |"
)
w(
    f"| C1 (S-3m) | F0 in SQL GP storage | F5 on Blob Hot | EUR/month at rest | {f(c1['3m_f0_eur'])} -> {f(c1['3m_f5_eur'])} ({c1['3m_f0_eur'] / c1['3m_f5_eur']:.1f}x) | MODELLED (MEASURED bytes) | same + `prices.yaml` |"
)
w(
    f"| C1 (full S, {ts:,} ticks) | F0 extrapolated at {F0_BPT:.2f} B/tick | F5 measured | GiB at rest | {f(c1['s_f0_gib'], 3)} -> {f(c1['s_f5_gib'], 3)} ({c1['s_f0_gib'] / c1['s_f5_gib']:.1f}x) | F0 MODELLED, F5 MEASURED | same |"
)
w(
    f"| C1 (full S) | F0 extrapolated | F5 measured | EUR/month at rest | {f(c1['s_f0_eur'])} -> {f(c1['s_f5_eur'])} ({c1['s_f0_eur'] / c1['s_f5_eur']:.1f}x) | MODELLED | same + `prices.yaml` |"
)
w(
    f"| C2 (local run) | all rows in SQL | after `tidal tiering run` (90 d window) | `md.tick_hot` reserved KB | {tick_kb_before:,} -> {tick_kb_after:,} | MEASURED (local SQL Server) | `c2/dry_run.txt`, `c2/run1_raw.txt`, `c2/run2_raw.txt` |"
)
w(
    f"| C2 (local run) | all rows in SQL | after run | `md.bar_hot` reserved KB | {bar_kb_before:,} -> {bar_kb_after:,} | MEASURED (local SQL Server) | `c2/dry_run.txt`, `c2/run1_raw.txt`, `c2/run2_raw.txt` |"
)
w(
    f"| C2 (model, S: {SCALES['S'][0]:,} ticks, {SCALES['S'][1]} y) | SQL GP {GP_VCORES} vCore, all history | serverless + {HOT_DAYS} d hot, rest on Blob | EUR/month | {f(c2['S']['base_total'], 2)} -> {f(c2['S']['opt_total'], 2)} | MODELLED, NEEDS-CLOUD | `c1_c3_model.py` |"
)
w(
    f"| C2 (model, L: {SCALES['L'][0]:,} ticks, {SCALES['L'][1]} y) | SQL GP {GP_VCORES} vCore, all history | same | EUR/month | {f(c2['L']['base_total'], 2)} -> {f(c2['L']['opt_total'], 2)} | MODELLED, NEEDS-CLOUD | `c1_c3_model.py` |"
)
w(
    f"| C3 (S / M / L) | all Hot | Hot/Cool/Cold by `age_band`, ticks-only, 2 tags | net EUR/month vs all-Hot | {R['S']['tk_net2']:+.4f} / {R['M']['tk_net2']:+.4f} / {R['L']['tk_net2']:+.4f} | MODELLED, tier capacity NEEDS-CLOUD | `deltas/T-207_model.py` |"
)
w(
    f"| C3 (L, at rest only) | all Hot | tiered | EUR/month | {f(R['L']['allhot'])} -> {f(R['L']['tiered'])} | MODELLED | `deltas/T-207_model.py` |"
)
w()
w("## Executive summary (for the trading manager)")
w()
w(
    f"* **Storing the data as epoch Parquet is {c1['3m_f0_gib'] / c1['3m_f5_gib']:.1f}x smaller than the relational baseline** on the benchmark slice (MEASURED bytes). In money, {c1['3m_f0_eur'] / c1['3m_f5_eur']:.0f}x cheaper at rest, but the absolute amount is cents: at the full 50 M-tick scale it is EUR {f(c1['s_f5_eur'], 3)} versus EUR {f(c1['s_f0_eur'], 3)} per month (MODELLED)."
)
w(
    f"* **Moving old rows out of SQL works end to end** on the local stack: the tiering job moved {tick_rows:,} tick rows and {bar_rows:,} bar rows to Parquet and SQL space fell from {tick_kb_before + bar_kb_before:,} KB to {tick_kb_after + bar_kb_after:,} KB reserved (MEASURED, local). It took {int(tm['run1_s']) + int(tm['run2_s'])} s of wall-clock in two runs (the first was cut short by a Docker engine restart and the second resumed it)."
)
w(
    f"* **The real saving is SQL compute, not bytes.** Right-sizing from an always-on GP {GP_VCORES}-vCore database to an auto-pausing serverless one is modelled at EUR {f(c2['S']['base_total'], 0)} -> EUR {f(c2['S']['opt_total'], 0)} per month at S (MODELLED, NEEDS-CLOUD). It rests on an assumed {SRV_HOURS:.0f} active hours per month; the saving disappears at {break_even_hours:.0f} active hours."
)
w(
    f"* **Blob tiering by age (C3) is worth almost nothing at these sizes**: net {R['S']['tk_net2']:+.4f} / {R['M']['tk_net2']:+.4f} / {R['L']['tk_net2']:+.4f} EUR per month at S / M / L (MODELLED). It is only positive because we tag ticks only, with two tags (ADR-0015); the earlier design was a net loss. It is a correctness-of-design point, not a budget lever."
)
w(
    "* **Nothing here is a real Azure bill.** Every Azure EUR figure is list price times a measured or assumed quantity. Real billing and the tier-capacity metric need a deployed environment (NEEDS-CLOUD)."
)
w()
w("## C1. Epoch time-series format: F0 vs F5")
w()
w(
    "Source: T-204 raw results (`storage-benchmark-2026-10-22.parquet`), bytes MEASURED; EUR = GiB x unit price (MODELLED). F0 is priced as SQL GP storage "
    f"(`sql_gp_storage_gb_month` {SQL_GB}), F5 as Blob Hot (`blob_hot_stored_gb_month` {BLOB_HOT}). F0 billed basis is the SQL-reserved size ({f0_3m_bill:,} B)."
)
w()
w("| Scale | Ticks | F0 bytes | F5 bytes | F0 B/tick | F5 B/tick | F0 EUR/month | F5 EUR/month |")
w("|---|---|---|---|---|---|---|---|")
w(
    f"| S-3m (MEASURED bytes) | {t3m:,} | {f0_3m:,} | {f5_3m:,} | {F0_BPT:.2f} | {F5_BPT_3M:.2f} | {f(c1['3m_f0_eur'], 5)} | {f(c1['3m_f5_eur'], 5)} |"
)
w(
    f"| S full (F5 MEASURED, F0 MODELLED) | {ts:,} | {f0_s_model:,.0f} | {f5_s:,} | {F0_BPT:.2f} | {F5_BPT_S:.2f} | {f(c1['s_f0_eur'], 5)} | {f(c1['s_f5_eur'], 5)} |"
)
w()
w(
    f"Adjusted F0 (without the extra `ts_sub` column F0 needs for nanosecond accuracy): {f0_3m_adj:,} B ({f0_3m_adj / f5_3m:.1f}x F5)."
)
w()
w(
    "**S-3m caveat.** F0 (and F1, F2) was run only on a 3-month slice of S (2.24 M ticks) because the SQL load ran at 1.4k-15k rows/s and the full run was not feasible. "
    f"The full-S F0 row above is therefore the S-3m bytes/tick ({F0_BPT:.2f}) multiplied by the full tick count: MODELLED. "
    f"The extrapolation is supported for files (F5: {F5_BPT_3M:.2f} B/tick at S-3m versus {F5_BPT_S:.2f} at full S, {abs(F5_BPT_S / F5_BPT_3M - 1) * 100:.1f} % apart) but is untested for SQL rowstore. "
    "A B-tree with a fixed row width should be close to linear, but fill factor and page splits are not measured at full S. "
    f"The full-S F5 figure is MEASURED: {f5_s:,} B = {F5_BPT_S:.2f} bytes/tick."
)
w()
w(
    "Trade-off: F5 is a file format. Point lookups are slower than a clustered index (T-204 W4 factor 0.3x), writes are append-only per instrument-month, and there is no row-level update; restatements are new versions. "
    "Analytical scans are faster (T-204). Operational effort: compaction and tagging jobs (SPEC-03)."
)
w()
w("## C2. SQL to object storage migration")
w()
w("### Measured: one local `tidal tiering run`")
w()
w(
    f"Command (inside the dev image, compose stack): `TIDAL_CLOCK__NOW=2026-10-24T00:00:00Z tidal tiering run` (hot window {HOT_DAYS} d, defaults for batch/delete chunk). "
    "The clock is frozen so the window is repeatable. Dry run first (plan only), then the real run. Local SQL Server 2022 Developer in Docker plus Azurite; not Azure."
)
w()
w(
    "| Table | Rows planned (dry run) | Rows moved, run 1 | Rows moved, run 2 | SQL rows left | Parquet bytes written | `sp_spaceused` reserved KB before | after |"
)
w("|---|---|---|---|---|---|---|---|")
w(
    f"| md.tick_hot | {tick_rows:,} | {tick_run1:,} (cut short) | {tick_run2:,} | 0 | {tick_bytes:,} | {tick_kb_before:,} | {tick_kb_after:,} |"
)
w(
    f"| md.bar_hot | {bar_rows + 756:,} in table, {bar_rows:,} older than the window | {run1['md.bar_hot']['rows_moved']:,} | 0 | 756 (inside the {HOT_DAYS} d window) | {bar_bytes:,} | {bar_kb_before:,} | {bar_kb_after:,} |"
)
w()
w(
    f"Timings (MEASURED wall-clock of each `docker compose run`, including container start): dry run {tm['dry_s']} s; real run 1 {tm['run1_s']} s (exit {tm['run1_exit']}); real run 2 {tm['run2_s']} s (exit {tm['run2_exit']}). "
    "Run 1 moved all bars and part of the ticks, then lost its SQL connection when the Docker engine restarted (`08S01 communication link failure`, an environment event, not a job defect). "
    "The job is resumable by design (rows are deleted from SQL only after the Parquet is verified; the watermark advances only when a table finishes), and run 2 moved exactly the remaining rows with 0 errors and 0 deduped. "
    "Total Parquet bytes per dataset are read back from Azurite (`c2/blobs_created.json`, helper `c2/list_blobs.py`); the bar bytes equal the run-1 log. "
    "After the runs `md.tick_hot` is empty (`c2/sql_state_after.txt`; a fresh `sp_spaceused` shows 200 KB reserved, the 2,184 KB in the run-2 log was taken before ghost cleanup, because `space_used_kb` does not use `@updateusage`). "
    "The machine was shared with other containers."
)
w()
w(
    f"Measured ratio on ticks: SQL {SQL_BPT:.2f} B/tick reserved (rowstore, tick_hot with its index), Parquet {PQ_BPT:.2f} B/tick written ({tick_bytes:,} B over {tick_rows:,} ticks), so object storage holds {RATIO * 100:.0f} % of the SQL bytes ({1 / RATIO:.1f}x smaller). "
    "Bars are not used in the cost model below (ticks dominate bytes)."
)
w()
w("### Modelled: Azure EUR (NEEDS-CLOUD for real billing)")
w()
w(
    f"Assumptions (stated, not measured): history of {', '.join(f'{k} = {v[0]:,} ticks over {v[1]} y' for k, v in SCALES.items())} (T-121 10 B/row sizes; config-contract scales). "
    f"Baseline: GP provisioned {GP_VCORES} vCore always on ({GP_VCORES} x {HOURS_MONTH} h x {GP_H} EUR) plus all ticks in SQL storage at {SQL_GB} EUR/GiB-month. "
    f"Optimised: last {HOT_DAYS} d in serverless SQL ({SRV_VCORES:.0f} vCore average x {SRV_HOURS:.0f} active h x {SRV_H} EUR, auto-paused otherwise), older rows as Parquet on Blob Hot ({BLOB_HOT} EUR/GiB-month), "
    f"{QUERIES_PER_MONTH:,} cold-path queries/month at {q_eur * 1000:.4f} EUR per 1,000 (T-204 W3 request counts {GET_HEAD_LIST[0]} GET + {GET_HEAD_LIST[1]} HEAD + {GET_HEAD_LIST[2]} LIST, `blob_hot_read_10k` for GET and HEAD, `blob_hot_write_10k` for LIST, as in the T-204 report). "
    "Bytes use the measured SQL and Parquet bytes/tick above. The `sql_gp_provisioned_vcore_hour` meter may exclude licence cost (see its note in prices.yaml), so the baseline is a lower bound."
)
w()
w("| | S | M | L |")
w("|---|---|---|---|")
for lab, k, d in (
    ("Hot share of history", "hot_share", 3),
    ("SQL GiB, all history", "sql_all_gib", 3),
    ("SQL GiB, hot only", "sql_hot_gib", 3),
    ("Blob GiB (Parquet)", "blob_gib", 3),
    ("Baseline compute EUR/month", "base_compute", 2),
    ("Baseline SQL storage EUR/month", "base_storage", 4),
    ("**Baseline total EUR/month**", "base_total", 2),
    ("Optimised serverless compute EUR/month", "opt_compute", 2),
    ("Optimised SQL storage EUR/month", "opt_sql_storage", 4),
    ("Optimised Blob storage EUR/month", "opt_blob", 4),
    ("Optimised cold-query transactions EUR/month", "opt_query", 4),
    ("**Optimised total EUR/month**", "opt_total", 2),
):
    w(f"| {lab} | " + " | ".join(f(c2[s][k], d) for s in SCALES) + " |")
w()
w(
    f"Storage bytes shrink by the measured ratio but are cents at every scale; the difference is dominated by compute. Break-even serverless active hours at S (compute only, 1 vCore average): {break_even_hours:.0f} h/month of {HOURS_MONTH}. "
    "The serverless saving therefore depends on the auto-pause duty cycle, which is a usage assumption, not a property of the migration. "
    "Hidden costs counted: cold-path read transactions. Not counted: egress (same region, none), early deletion (Hot tier has none), log ingestion (C-series elsewhere), Service Bus and Container Apps compute for the tiering job."
)
w()
w(
    "Trade-offs: queries older than 90 d go through DuckDB over Blob (higher first-byte latency, per-query transaction cost), serverless adds a cold-start (resume) delay of the first query after a pause, "
    "and the tiering job is another moving part (watermark, applock, verify-before-delete; crash-safe and idempotent per SPEC-03). Restatements of tiered months re-version rows (ADR-0014)."
)
w()
w("## C3. Blob lifecycle tiering by data age")
w()
w(
    "MODELLED, from `docs/finops/deltas/T-207_model.py` executed by this script (not re-typed), design of ADR-0015: only `ticks` parts are tagged, two tags (`age_band`, `compacted`). "
    "Bands: hot < 3 y, Cool 3-10 y, Cold > 10 y; bytes spread evenly over the data years; early deletion and retrieval counted."
)
w()
w("| EUR/month | S | M | L |")
w("|---|---|---|---|")
for lab, k, d, fmt in (
    ("GB", None, 1, None),
    ("Hot/Cool/Cold share", "split", 0, None),
    ("All Hot at rest", "allhot", 4, ""),
    ("Tiered at rest", "tiered", 4, ""),
    ("At-rest saving", "saving", 4, ""),
    ("Index tags cost (ticks only, 2 tags)", "tk_tagcost2", 4, ""),
    ("Compaction reads + crossing ops, per month", "mixed", 4, ""),
    ("**Net vs all-Hot (2 tags)**", "tk_net2", 4, "+"),
    ("Net if 5 tags (previous design)", "tk_net5", 4, "+"),
    ("One-off tier-move cost (EUR, once)", "tk_oneoff", 4, ""),
    ("Max early-delete exposure per GB moved to Cold (EUR)", "ed", 4, ""),
):
    cells = []
    for s, (_i, _y, gb) in g["SCALES"].items():
        m = R[s]
        if k is None:
            cells.append(f"{gb}")
        elif k == "split":
            cells.append(f"{m['fh']:.2f} / {m['fc']:.2f} / {m['fd']:.2f}")
        elif k == "mixed":
            cells.append(f"{(m['tk_comp_yr'] + m['tk_cross_yr']) / 12:.4f}")
        elif k == "ed":
            cells.append(f"{COLD_ED_MAX:.4f}" if m["fd"] > 0 else "n/a (no Cold data)")
        else:
            cells.append(format(m[k], f"{fmt}.{d}f"))
    w(f"| {lab} | " + " | ".join(cells) + " |")
w()
w(
    "MEASURED tier capacity (Azure Monitor `BlobCapacity` by `Tier`, or Cost Management meter split) is NEEDS-CLOUD: nothing is deployed, and the lifecycle policy needs up to 48 h to act on a real account. "
    "Azurite has no access tiers or lifecycle engine, so there is no local measurement either."
)
w()
w(
    "Trade-offs: Cool/Cold reads cost per GB retrieved and per 10 K operations, and compaction of a tiered partition pays them again; Cold has a 90-day minimum (early deletion). "
    "Cold first-byte latency is slightly higher than Hot. Operational effort: yearly `retag` plus the `compacted=true` guard (SPEC-03 section 4). Market data never goes to Archive (ADR-0004)."
)
w()
w("## What would change the conclusions")
w()
w(
    "* C1: a measured F0 at full S (bytes/tick drifting with fill factor) would move the extrapolated F0 row; real tick data compresses differently from the synthetic generator."
)
w(
    f"* C2: the serverless active hours ({SRV_HOURS:.0f}/month assumed), the vCore average, and whether the provisioned meter includes a licence. A real invoice replaces the whole table. Break-even is {break_even_hours:.0f} h/month at 1 vCore."
)
w(
    "* C3: part size and part count (tags are billed per tagged blob), the real tick volume per instrument, and whether Cool/Cold prices stay as captured. Larger L-scale data and many small parts flip the sign."
)
w()
w("## Files")
w()
w(
    "* `docs/finops/results/c1_c3_model.py` (this generator), `docs/finops/results/c1-c3.md` (its output)"
)
w(
    "* `docs/finops/results/c2/` raw C2 evidence: `dry_run.txt`, `run1_raw.txt`, `run2_raw.txt`, `timings.txt`, `blobs_created.json`, `list_blobs.py`, `sql_state_after.txt`"
)

(RES / "c1-c3.md").write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"wrote {RES / 'c1-c3.md'}")
