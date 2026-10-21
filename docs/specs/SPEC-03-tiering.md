# SPEC-03 — Hot/Cold Tiering & SQL → Object Storage Migration

Owner agent: `data-engineer` (job) + `infra-engineer` (lifecycle policy) · Contracts: storage-layout §1,§4,
messaging-contract `tiering.job/v1`, data-contracts §6 · Tasks: T-205 … T-208

## 1. Purpose
Keep only the **hot window** (default 90 days) in Azure SQL; move older rows to epoch Parquet on Blob;
let Blob lifecycle push old partitions down to Cool → Cold by **data age** (Archive only for `raw/`). Demonstrates
"moving data from structured relational DB to object storage" and "optimise storage costs".

## 2. Migration job (`storage/tiering.py`, run as Container Apps Job)
1. Acquire `sp_getapplock('tiering:' + table, 'Exclusive')`; exit if held.
2. Read watermark from `ops.migration_watermark`.
3. Select rows (**all versions**) with `ts_ns < clock.now() − hot_window` and `ts_ns ≥ watermark`, in
   **monthly batches** (`batch_rows` cap), ordered by `instrument_key, ts_ns, version`. Daily bars append
   one part per month to their yearly partition (storage-layout §2).
4. Write Parquet via `parquet_writer` to the layout path; write `_manifest.json` (row count, min/max ts,
   sha256 of file).
5. **Verify**: re-read the written file, compare row count + checksum of (ts_ns, close) with the SQL batch.
6. Delete the migrated range in chunks of 50k rows, one short transaction per chunk (avoids log
   blow-up); deletes are bounded by the batch's ts range, so re-running them is safe. Then advance the
   watermark in its own transaction.
7. Set blob index tags (`age_band` computed from `data_year`).
8. Emit metrics: rows moved, bytes written, SQL space reclaimed (`sp_spaceused` before/after).

Idempotency: if a crash happens after step 4 but before 6, the next run detects the manifest and
skips the write, only deleting/advancing. Property test: kill the job at each step → final state identical.

## 3. Yearly re-tagging
`tidal tiering retag` recomputes `age_band` for all blobs once per year (Jan 2) so lifecycle rules move
data that "aged" across a band boundary.

## 3.1 Compaction
`tidal tiering compact --dataset bars_1d` merges the parts of a partition into one file keeping every
version (storage-layout §2). Runs yearly after retag, and is crash-safe the same way as migration.

## 4. Lifecycle policy (Terraform `modules/storage`)

| Rule | Filter | Action |
|---|---|---|
| md-cool | container `marketdata`, tag `age_band = cool` | tierToCool after 0 days since modification |
| md-cold | tag `age_band = cold` | tierToCold after 0 days |
| raw | prefix `raw/` | Cool @ 30 d, Archive @ 180 d |
| backtests | prefix `backtests/` | Cool @ 30 d, delete @ 365 d |
| exports / bench | prefix | delete @ 14 / 30 d |

**Archive is never used for `marketdata/`** — rehydration takes hours and breaks backtests; documented as
a deliberate trade-off (ADR-0004). Early-deletion penalties (Cool 30 d, Cold 90 d, Archive 180 d) are
modelled in the cost model.

## 5. Unified reader (`query/reader.py`)
- `read_bars(instrument, interval, start, end, as_of=None)` splits the range at the watermark:
  cold part via duckdb `read_parquet('az://marketdata/v1/...', hive_partitioning=1)` with filter pushdown,
  hot part via SQL; concatenates with polars; resolves versions per data-contracts §7 (latest, or
  max version with `ingested_at_ns ≤ as_of`).
- Returns tier used (`hot|cold|mixed`) for the `X-Tidal-Tier` header.

## 6. Acceptance criteria
- [ ] After migration, SQL contains only rows inside the hot window; Parquet + SQL union = original (row-for-row, hash).
- [ ] Crash-injection property test passes for every step.
- [ ] SQL storage before/after recorded in `docs/finops/results/`.
- [ ] Lifecycle policy applied via Terraform; a test blob tagged `cool` moves tier within 48 h (screenshot).
- [ ] Reader result for a range spanning the watermark equals pure-SQL baseline (hypothesis test, 200 random ranges).
