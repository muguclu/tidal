# SPEC-02 — Epoch Time-Series Storage Format & Benchmark

Owner agent: `data-engineer` (+ `finops-analyst` for € columns) · Contracts: data-contracts §2–3,
storage-layout §2–3 · Tasks: T-201 … T-204

## 1. Purpose
Prove with numbers that storing time series as **sorted epoch-int64 Parquet with delta/byte-stream-split
encoding** is smaller, faster and cheaper than the alternatives. This benchmark is the centrepiece of the
"Save Time-Series Data efficiently" requirement.

## 2. Candidates

| ID | Format | Notes |
|---|---|---|
| F0 | Azure SQL rowstore, `DATETIME2` + `DECIMAL(18,6)`, no compression | naive baseline |
| F1 | Azure SQL rowstore, `ts_ns BIGINT`, PAGE compression | tuned relational |
| F2 | Azure SQL **clustered columnstore** | strongest relational option (fair comparison) |
| F3 | CSV gzip on Blob | common "data lake" anti-pattern |
| F4 | Parquet default (snappy, `timestamp[us]`, plain floats) | naive Parquet |
| F5 | **Parquet epoch-optimised** (storage-layout §3) | Tidal format |
| F6 | F5 + zstd level sweep (3/6/9) and row-group sweep (128k/1M/4M) | tuning |

## 3. Workloads
W1 bulk write 1 instrument × 20 yrs ticks · W2 full scan + daily OHLC aggregation (1 instrument) ·
W3 range query 1 month · W4 point lookup last price · W5 cross-instrument scan, 10 instruments × 1 yr ·
W6 as-of query with restatements.

## 4. Metrics (per format × workload)
Bytes on storage · write throughput (rows/s) · wall time p50/p95 (5 runs, cold & warm cache) ·
bytes read from storage (duckdb profiling / SQL `STATISTICS IO`) · CPU seconds · **€/month at rest**
(Blob tier prices or SQL storage price) · **€ per 1,000 queries** (compute + transactions + egress).

Prices are read from a pinned `docs/finops/prices.yaml` (region `westeurope`, date captured) —
never hard-coded in code.

## 5. Method
- Runner: `benchmarks/storage/run.py --scale S|M|L --formats F0..F6 --workloads W1..W6`.
- Local first (Azurite, SQL Server container) for relative numbers; then **one** Azure run at M scale for absolute
  numbers (SQL serverless 2 vCore, Blob Hot, query from Container Apps 1 vCPU/2 GiB).
- Results to `docs/finops/results/storage-benchmark-{date}.md` + `.parquet` (raw) via a template;
  charts rendered with matplotlib to `docs/finops/results/img/`.
- Explain *why* F5 wins: delta encoding of monotonic timestamps ≈ 1–2 bytes/row, byte-stream-split
  makes float bytes compressible, min/max stats enable row-group skipping.

## 6. Acceptance criteria
- [ ] All 7 formats × 6 workloads produce numbers at S and M scale.
- [ ] Results reproducible: same scale + seed ⇒ sizes within 1 %, timings within 15 %.
- [ ] Report has an executive summary (3 bullets, business language) + methodology + caveats.
- [ ] F5 vs F0 size ratio and € ratio stated explicitly in README.
- [ ] Benchmark code has tests for correctness (all formats return identical aggregates for W2).
