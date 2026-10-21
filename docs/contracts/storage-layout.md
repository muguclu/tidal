# Storage Layout Contract — v1

Code mirror: `src/tidal/storage/layout.py` — **the only place paths are built.** Any string
concatenation of blob paths elsewhere fails code review.

## 1. Storage account & containers

One StorageV2 account, LRS, default access tier `Hot`, **hierarchical namespace disabled**
(flat namespace) so that blob index tags can drive lifecycle rules (§4). Verify current tag support
for HNS accounts before changing this; the trade-off is recorded in ADR-0004.

| Container | Purpose | Lifecycle |
|---|---|---|
| `raw` | Verbatim source responses (JSON/CSV), gzip | → Cool @ 30 d → Archive @ 180 d |
| `md` | Market data Parquet (cold store) | tier by **data age** via blob index tag, see §4 |
| `reference` | Instruments, calendars | Hot, no lifecycle |
| `backtests` | Job inputs/outputs | → Cool @ 30 d → delete @ 365 d |
| `exports` | Legacy CSV feed from VM | delete @ 14 d |
| `bench` | Benchmark artefacts | delete @ 30 d |
| `tfstate` | (separate account) Terraform state | versioning + soft delete |

## 2. Market-data paths

```
md/v1/{dataset}/instrument={instrument_id}/year={YYYY}/month={MM}/part-{seq:05d}.parquet
  dataset ∈ { ticks, bars_1s, bars_1m, bars_1h, bars_1d }
```

- Hive-style partitions so duckdb/polars prune by instrument & time.
- Daily bars: one file per instrument per **year** (`month=00`) — tiny data, avoid small files.
- Target file size **64–256 MB**; writer rolls files at 1M rows (ticks) or by month.
- Files are **immutable**. Restatements write a new `part-` with higher `version` rows; compaction
  job merges and keeps latest version per key (`compacted=true` tag).

## 3. Parquet writer settings (normative)

| Setting | Value |
|---|---|
| compression | `zstd`, level 6 (benchmarked vs 3/9 in SPEC-02) |
| row group size | 1,000,000 rows |
| data page size | 1 MiB |
| `ts_ns` encoding | `DELTA_BINARY_PACKED` |
| float encodings | `BYTE_STREAM_SPLIT` (+ zstd) |
| dictionary | on for `instrument_id`, `interval`; off for floats |
| statistics | on (min/max per row group) — enables predicate pushdown |
| sort order | `ts_ns` ascending, declared in `sorting_columns` metadata |
| key-value metadata | `tidal.schema=TICK_V1`, `tidal.writer_version`, `tidal.source_hash` |

## 4. Blob index tags (used for tiering & discovery)

| Tag | Values |
|---|---|
| `dataset` | ticks, bars_1d, … |
| `instrument` | instrument_id |
| `data_year` | YYYY |
| `age_band` | `hot` (< 3 y) · `cool` (3–10 y) · `cold` (> 10 y) — set by tiering job yearly |
| `compacted` | true/false |

Lifecycle rules filter on `age_band` tags (not on last-modified, because a backfill writes 20-year-old
data "today"). This is a deliberate design point — see ADR-0004.

## 5. Naming rules

- All lowercase except `instrument=` values (which keep contract casing).
- No spaces; `-` in file names, `_` in partition values.
- Local dev mirrors the same layout under `./.data/` (Azurite or local FS via fsspec).
