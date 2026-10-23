# SPEC-03 — Hot/Cold Tiering & SQL → Object Storage Migration

Owner agent: `data-engineer` (job) + `infra-engineer` (lifecycle policy) · Contracts: storage-layout §1,§4,
messaging-contract `tiering.job/v1`, data-contracts §6 · Tasks: T-205 … T-208

## 1. Purpose
Keep only the **hot window** (default 90 days) in Azure SQL; move older rows to epoch Parquet on Blob;
let Blob lifecycle push old partitions down to Cool → Cold by **data age** (Archive only for `raw/`). Demonstrates
"moving data from structured relational DB to object storage" and "optimise storage costs".

## 2. Migration job (`storage/tiering.py`, run as Container Apps Job)
Per table (`md.bar_hot`, `md.tick_hot`):
1. Acquire `sp_getapplock('tiering:' + table, 'Exclusive')`; exit if held. The job re-checks
   `APPLOCK_MODE` on the lock session before every delete chunk and stops (`LockLost`) if it lost it.
   Compaction and retag take the same lock.
2. Read the watermark from `ops.migration_watermark`; `target = month_start(clock.now() − hot_window)`.
3. Per series (instrument, interval), oldest first, per **calendar month**: select rows (**all
   versions**) with `ts_ns < target` ordered `instrument_key, ts_ns, version`, in pages of
   `batch_rows`, never splitting one timestamp. There is **no lower bound** (`ts_ns ≥ watermark`
   would hide late restatements): rows that reach SQL below the watermark are swept the same way
   into a new `part-`. Daily bars append one part per month to their yearly partition (storage-layout §2).
4. Plan against cold: read the existing cold rows of the month once (parts whose manifest min/max
   overlaps; a manifest entry whose recorded size differs from the listed size is ignored) and drop
   what is already there. Idempotency is by **content**, not by a marker; rules (ADR-0014): hot
   rows newer than the key's whole cold history are compared with the cold row of the max version
   (equal: only deleted; different, also a revert to an earlier value: new version `max + 1`,
   `RESTATED`); other hot rows are already cold iff cold has the same key, version and values with
   `cold.ingested_at_ns ≤ hot.ingested_at_ns`; any other same key + version mismatch is a
   `TieringConflict` (nothing deleted, watermark unchanged).
5. Write the planned rows of the month in **one** `parquet_writer` call (rolls at `MAX_PART_ROWS`).
6. **Verify**: re-read the written parts, compare row count + checksum of (`ts_ns`, `close|price`)
   with the planned rows. On a mismatch delete the parts just written, then fail.
7. Set blob index tags (`age_band` from the path's year, `compacted`; `ticks` only, ADR-0015) and rebuild `_manifest.json`
   (derived, replaceable, storage-layout §2). Tags are set **before** the delete so that no state
   exists with deleted SQL rows and untagged parts.
8. Delete the month from SQL in chunks of 50k rows, one short transaction per chunk, each bounded by
   the ts range of the rows that were read (a chunk that would remove more rows than were read is
   rolled back). Then advance the watermark to `max(old, target)` in its own transaction.
9. Emit metrics: rows moved / deduped / swept, bytes written, SQL space (`sp_spaceused` without
   `@updateusage`; `None` with a warning when the login may not run it).

**Frozen clock.** With a frozen `TIDAL_CLOCK__NOW` a restatement of a migrated key is stamped with the
same `ingested_at_ns` as the cold history, is not "newer", and therefore ends as a `TieringConflict`.
Demos on a frozen clock must not restate migrated keys.

`--dry-run` (`tiering.job/v1.dry_run`) runs 1–4 and reports; it changes nothing.

Idempotency and resume: any step can be killed. Nothing leaves SQL before its rows are verified in
cold; parts are created atomically and never modified; a re-run recomputes the plan from the content
of both tiers, so rows already written produce an empty write and the run only tags, rebuilds the
manifest and deletes. The step order and the argument per crash point are in the `tiering.py`
module docstring; T-206 injects a crash at every step including between delete chunks and between
the manifest delete and put.

## 3. Yearly re-tagging
`tidal tiering retag` recomputes `age_band` for all blobs once per year (Jan 2) so lifecycle rules move
data that "aged" across a band boundary. It runs per dataset under the same table lock as the
migration (a busy dataset is skipped and reported) and preserves `compacted`. Only datasets in
`retag.TAGGED_DATASETS` (default `ticks`, ADR-0015) are tagged, with `age_band` and `compacted` only; the
year comes from the path. Retag also removes the retired `dataset`/`instrument`/`data_year` tags and all
tags of untagged datasets (migration of blobs written by earlier versions). Idempotent.

## 3.1 Compaction
`tidal tiering compact --dataset bars_1d` merges the parts of a partition into one file (several if
it exceeds `MAX_PART_ROWS`) keeping every version (storage-layout §2). Rows equal in key, version and
values collapse to the earliest `ingested_at_ns`; the same key + version with other values fails the
partition without touching it. Runs yearly after retag under the table lock and is crash-safe the
same way as migration: nothing is deleted before the merged parts are verified, a re-run recognises
the complete set of output parts and resumes at tag + delete.

Compaction runs **yearly for every dataset** (`ticks`, `bars_1s`, `bars_1m`, `bars_1h`, `bars_1d`)
immediately after retag, in the same scheduled job (T-309). For a tagged dataset (`ticks`) every kept
part ends with `compacted=true`; that tag is what makes it eligible for the lifecycle tiering of §4. Untagged
datasets are compacted (small-file control) but no tag call is made; they stay Hot. A partition that is
already a single part is skipped without reading data: `compacted=true` (ticks), or a manifest listing
exactly that one part (untagged datasets).

## 4. Lifecycle policy (Terraform `modules/storage`)

| Rule | Filter | Action |
|---|---|---|
| md-cool | container `marketdata`, prefix `v1/`, blockBlob, tags `age_band = cool` AND `compacted = true` | tierToCool after 0 days since modification |
| md-cold | container `marketdata`, prefix `v1/`, blockBlob, tags `age_band = cold` AND `compacted = true` | tierToCold after 0 days (never Archive) |
| raw | prefix `raw/` | Cool @ 30 d, Archive @ 180 d |
| backtests | prefix `backtests/` | Cool @ 30 d, delete @ 365 d |
| exports / bench | prefix | delete @ 14 / 30 d |

Only `ticks` parts carry the tags (ADR-0015), so only tick data is tiered; `bars_*` stay Hot. A dataset joins
when its measured average part size clears the 2-tag break-even (`docs/finops/deltas/T-207.md`).

**Compaction guard.** md-cool and md-cold also require `compacted = true`. Compaction deletes the old
parts of a partition after writing the merged one; a part moved to Cool/Cold before that would be billed
the early-deletion fee (Cool 30 d, Cold 90 d) and a tier-change write for bytes about to be deleted. The
cost is that a partition leaves Hot only after its first compaction, and a restated (new, uncompacted)
part stays Hot until the next yearly run. `_manifest.json` is never tagged, so no rule can match it and it
stays Hot. A partition whose compaction fails (`TieringConflict`) stays Hot and is reported. Cost numbers, including the per-tag storage charge, are in `docs/finops/deltas/T-207.md`.

**Archive is never used for `marketdata/`** — rehydration takes hours and breaks backtests; documented as
a deliberate trade-off (ADR-0004). Early-deletion penalties (Cool 30 d, Cold 90 d, Archive 180 d) are
modelled in the cost model.

## 5. Unified reader (`query/reader.py`)
- `read_bars(instrument, interval, start, end, as_of=None)` reads **SQL first, then Parquet, both over the
  whole range** — cold via duckdb `read_parquet('az://marketdata/v1/...', hive_partitioning=1)` with
  filter pushdown — concatenates with polars and resolves versions per data-contracts §7 (latest, or max
  version with `ingested_at_ns ≤ as_of`), which de-duplicates rows present in both tiers. Reading SQL first
  and both tiers in full is what keeps reads correct mid-migration (Parquet is written before the SQL delete,
  the watermark advances last) and picks up late restatements written to SQL below the watermark (T-208).
  The watermark only decides the tier label.
- Returns tier used (`hot|cold|mixed`) for the `X-Tidal-Tier` header. Streaming reads (`stream_*`) carry two tiers:
  `RowStream.tier` is the up-front label of the requested range (arrow/parquet headers are sent before any data), and
  the stream's `observed` tier is `mixed` as soon as a slice held SQL rows below the watermark or Parquet rows at or
  above it. JSON paging reads the observed tier after its page is read; arrow/parquet keep the up-front label.

## 6. Acceptance criteria
- [ ] After migration, SQL contains only rows inside the hot window rounded down to month start; Parquet + SQL union = original (row-for-row, hash).
- [ ] Crash-injection test passes for every step (T-206), including between delete chunks and between the
      manifest delete and put. **"Same final state" means**: the same SQL rows, the same cold rows
      including `version`, `ingested_at_ns` and `quality` (so `as_of` reads agree), and the same
      watermarks. File layout is not part of the criterion (T-205's own tests also assert it).
- [ ] SQL storage before/after recorded in `docs/finops/results/`.
- [ ] Lifecycle policy applied via Terraform; a test blob tagged `age_band=cool` and `compacted=true` moves tier within 48 h (screenshot).
- [ ] Reader result for a range spanning the watermark equals pure-SQL baseline (hypothesis test, 200 random ranges).
