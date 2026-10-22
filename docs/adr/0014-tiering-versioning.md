# ADR-0014: Versions and as-of reads when tiering meets rows that are already cold

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ orchestrator, data-engineer)
- Related: SPEC-03 §2/§3.1/§6, data-contracts §7, ADR-0013, T-205, T-206, T-208

## Context
The migration job (T-205) moves every version of every hot row to Parquet and deletes it from SQL.
Two situations make "the same key and version" ambiguous, and the code review reproduced both:

1. **A restatement arrives after its key was migrated.** SQL no longer holds the key, so the
   repository files the restated bar as `version = 1` (data-contracts §7 numbers versions from the
   rows it sees). Cold already holds `version = 1` with other values. Without a rule the job can only
   fail, and every restatement of old data would block the series.
2. **The same bar exists in both tiers with different `ingested_at_ns`.** ADR-0013 notes that a
   re-run of `tidal synth daily-history` with a later clock writes bars to cold that were hot before.
   Treating "same key, version, values" as already cold silently drops the hot copy. If the cold copy
   was ingested *later*, an `as_of` read at an earlier time sees the bar before migration (hot copy)
   and does not see it after (cold copy not yet ingested at that time).

`ingest/router.py` already solves the first case for the cold write path (`_plan_cold`: changed
value => `latest + 1`, `RESTATED`). Tiering must not behave differently from the router.

## Options
1. **Fail on any mismatch** (first implementation). Safe, but every restatement of migrated data
   becomes a permanent `TieringConflict`; the job never advances. Rejected.
2. **Re-version the hot row** to follow the cold history, like the router. Deterministic, keeps all
   data, needs a rule for "which row is newer".
3. **Make SQL version-aware of cold** (look cold up on every upsert). Moves tiering concerns into
   the hot path and makes ingest depend on Blob. Rejected.
4. **Ignore `ingested_at_ns` when deciding "already cold"**. Simplest, loses `as_of` history
   (case 2). Rejected.

## Decision
Option 2, with `ingested_at_ns` as the tie-breaker. For a hot row `h` and the cold rows of the same
key (`ts_ns` [+ `side`]):

* **Already cold** (the hot row is only deleted) iff cold holds the same key, version and values and
  `cold.ingested_at_ns <= h.ingested_at_ns` (the earliest ingestion of equal rows counts). A cold
  copy ingested later does not count: the hot row is written too, as a second row with the same key
  and version. Readers resolve them to the same values; `as_of` keeps seeing the earlier one.
* **Re-versioned copy already cold**: same key, values and `ingested_at_ns` at a version `>=` the hot
  version. This is how a resumed run recognises rows it re-versioned before the crash.
* **Same key and version, other values, and `h.ingested_at_ns` greater than every cold
  `ingested_at_ns` of the key**: the hot rows of the key (ordered by version, `ingested_at_ns`) are
  written as `max(cold max version, hot max version) + 1, +2, ...` with the `RESTATED` flag set.
  `ingested_at_ns` and values are untouched.
* **Any other same key + version mismatch** raises `TieringConflict`; nothing is written or deleted
  for that series and the watermark stays.
* **Compaction** collapses rows with equal key, version and values to the one with the **minimum**
  `ingested_at_ns`, and raises `TieringConflict` for the same key and version with different values
  (no silent loss).

`as_of = T` (data-contracts §7) is unchanged: per key, the max version among rows with
`ingested_at_ns <= T`. Duplicated equal rows with different `ingested_at_ns` are therefore harmless
and are exactly what keeps early `as_of` reads correct until compaction merges them (keeping the
earliest).

## Consequences
* Restatements of migrated history flow into cold without manual work; versions stay monotone.
* A re-versioned row's version differs from the version SQL gave it. Nothing else references the SQL
  version (it is deleted), but the number is not stable between SQL and cold: document in
  data-contracts §7 (changelog entry added with this ADR).
* Writing both copies of a duplicate costs a few rows of storage until compaction.
* The rule needs `ingested_at_ns` to be monotone per source. Equal timestamps with different values
  are treated as a conflict on purpose.
* T-206 compares "same final state" as: same SQL rows, same cold rows including `version`,
  `ingested_at_ns` and `quality`, same watermarks.
* Revisit if ingest ever writes concurrently from several writers with unsynchronised clocks.
