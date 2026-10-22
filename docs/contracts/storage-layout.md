# Storage Layout Contract — v1

Code mirror: `src/tidal/storage/layout.py` — **the only place paths are built.** Any string
concatenation of blob paths elsewhere fails code review.

## 1. Storage account & containers

Container names follow Azure rules (3–63 chars, lowercase): the market-data container is `marketdata`
(v1.1, ADR-0010 — `md` was too short to create). SQL keeps its `md` schema; only the Blob container is renamed.

One StorageV2 account, LRS, default access tier `Hot`, **hierarchical namespace disabled**
(flat namespace) so that blob index tags can drive lifecycle rules (§4). Verify current tag support
for HNS accounts before changing this; the trade-off is recorded in ADR-0004.

| Container | Purpose | Lifecycle |
|---|---|---|
| `raw` | Verbatim source responses (JSON/CSV), gzip | → Cool @ 30 d → Archive @ 180 d |
| `marketdata` | Market data Parquet (cold store) | tier by **data age** via blob index tag, see §4 |
| `reference` | Instruments, calendars | Hot, no lifecycle |
| `backtests` | Job inputs/outputs | → Cool @ 30 d → delete @ 365 d |
| `exports` | Legacy CSV feed from VM | delete @ 14 d |
| `bench` | Benchmark artefacts | delete @ 30 d |
| `tfstate` | (separate account) Terraform state | versioning + soft delete |

## 2. Market-data paths

```
marketdata/v1/{dataset}/instrument={instrument_id}/year={YYYY}/month={MM}/part-{seq:05d}.parquet
  dataset ∈ { ticks, bars_1s, bars_1m, bars_1h, bars_1d }
```

- Hive-style partitions so duckdb/polars prune by instrument & time.
- Daily bars: partition per instrument per **year** (`month=00`) — tiny data, avoid small files.
  Backfill writes one part per year; tiering appends one small part per migrated month, so a
  year accumulates ≤ 12 extra parts until compaction.
- Ticks and intraday bars: one file per instrument per **month** (roll to a new `part-` only above
  10M rows). Epoch Parquet is ~5–10 bytes/tick, so a month is typically 5–60 MB; bigger targets
  would need multi-month files and break monthly pruning.
- Each partition may hold a `_manifest.json` (`marketdata/v1/.../month=MM/_manifest.json`, built by
  `layout.md_manifest_path`): `dataset, instrument_id, year, month, rows, min_ts_ns, max_ts_ns` and,
  per part, `name, rows, min_ts_ns, max_ts_ns, bytes, sha256, source_hash`. It is **derived,
  replaceable and not read by the reader**: tiering uses it to skip parts that cannot overlap a month
  and rebuilds it (delete + create) after every change of the partition. A missing or stale manifest
  never changes query results.
- Files are **immutable**. Restatements write a new `part-` containing the higher-`version` rows.
  Compaction merges the parts of one partition into one file **keeping every version** (as-of needs
  them, data-contracts §7), writes it, verifies row count + checksum, then deletes the old parts and
  tags the new file `compacted=true`.

## 3. Parquet writer settings (normative)

| Setting | Value |
|---|---|
| compression | `zstd`, level 6 (benchmarked vs 3/9 in SPEC-02) |
| row group size | 1,000,000 rows (a file holds ≥ 1 row group) |
| data page size | 1 MiB |
| `ts_ns` encoding | `DELTA_BINARY_PACKED` |
| float encodings | `BYTE_STREAM_SPLIT` (+ zstd) |
| dictionary | on for `instrument_id`, `interval`; off for floats |
| statistics | on (min/max per row group) — enables predicate pushdown |
| sort order | `ts_ns` ascending, declared in `sorting_columns` metadata |
| key-value metadata | `tidal.schema=TICK_V1`, `tidal.writer_version`, `tidal.source_hash` |

## 4. Blob index tags (used for tiering)

Two tags, written on parts of the `ticks` dataset only (ADR-0015; `retag.TAGGED_DATASETS`). Parts of
`bars_*` and every `_manifest.json` are untagged and stay Hot.

| Tag | Values |
|---|---|
| `age_band` | `hot` (< 3 y) · `cool` (3–10 y) · `cold` (> 10 y) — set by tiering job yearly |
| `compacted` | true/false |

`dataset`, `instrument` and `data_year` are not tags: derive them from the path
(`layout.parse_md_path`). Blobs tagged by earlier versions are cleaned by `tidal tiering retag`.

Lifecycle rules filter on `age_band` and `compacted` tags (not on last-modified, because a backfill writes
20-year-old data "today"). This is a deliberate design point — see ADR-0004. Each tag is billed per month
in every tier, hence the minimum set (ADR-0015, `docs/finops/deltas/T-207.md`).

## 5. Naming rules

- Local dev (`LocalStore`) keeps blob index tags as JSON sidecars at
  `<root>/.tags/<container>/<blob name>.json`, outside every listed prefix. A sidecar is removed when
  its part is deleted (compaction, rollback of a failed write) so a recreated `part-` name starts
  untagged. On Azure the tags live on the blob and disappear with it.

- All lowercase except `instrument=` values (which keep contract casing).
- No spaces; `-` in file names, `_` in partition values.
- Local dev mirrors the same layout under `./.data/` (Azurite or local FS via fsspec).
