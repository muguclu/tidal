# ADR-0004: Epoch Parquet on flat-namespace Blob, tiering by data age via index tags, no Archive for market data

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ architect agent)
- Related: [SPEC-02](../specs/SPEC-02-storage-format.md), [SPEC-03](../specs/SPEC-03-tiering.md),
  [storage-layout](../contracts/storage-layout.md), [data-contracts §7](../contracts/data-contracts.md),
  [SPEC-06 §2](../specs/SPEC-06-security.md), [plan review #5, #9, #20, #24, #25](../reviews/2026-10-21-plan-review.md), T-205…T-208, T-503

## Context
Twenty-plus years of ticks are large, but traders mostly look at recent data: the last 90 days are read
many times a day, 3–10-year-old data a few times a week, 10+-year-old data a few times a year
(business-knowledge §7). The job requires **saving time-series data efficiently**, **moving data from
a relational DB to object storage**, and **cost optimisation**. Two traps: backfills write 20-year-old
data "today", so age by last-modified is wrong; and backtests and as-of queries need every restated
version.

## Options
1. **All history in Azure SQL.**
   - Pros: one store; simple queries. Cons: SQL storage and compute grow with history; free offer is 32 GB.
   - € impact: today's serverless hot window (0.5–2 vCore, 32 GB) is €5–20 list price (cost-model); the
     C2 baseline (all history in SQL GP 2 vCore, SPEC-09 C2) is measured in T-503.
2. **Epoch Parquet on Blob, lifecycle by last-modified.**
   - Pros: built-in lifecycle rules, no extra job. Cons: backfilled old data stays Hot.
   - € impact: to be measured in T-503 (C3).
3. **Epoch Parquet on flat-namespace Blob, lifecycle by blob index tag `age_band` from the data year.**
   - Pros: tiers follow how old the *data* is. Cons: flat namespace (no ADLS Gen2 HNS); yearly retag job.
   - € impact: storage €1–5/month at M scale (cost-model); target ≥ 80 % below all-hot SQL at L scale
     (PLANNING §8), measured in T-503 (C1, C3).
4. **Separate containers per tier.** Pros: explicit. Cons: manual moves, path changes, more code.
   - € impact: same at-rest prices as option 3 plus copy transactions; to be measured in T-503.

## Decision
Option 3, because only tiering by data age puts backfilled history in the cheap tiers while keeping
recent data fast.

## Consequences
- Hot window (90 d) stays in SQL; the migration job moves older rows, **all versions**, to Parquet.
- Ticks and intraday bars: one file per instrument per **month** (5–60 MB typical); daily bars: one per year.
- Compaction merges parts but **keeps every version** so `as_of` stays correct (data-contracts §7).
- **Archive is never used for `marketdata/`**: rehydration takes hours and breaks backtests. Only `raw/` archives.
- `id-tiering` needs **Storage Blob Data Owner** on `marketdata` (index tags need `tags/write`; ARM "Tag
  Contributor" does not cover it); T-403 checks whether a custom role is tighter.
- Monitor: capacity per tier, Cool/Cold read and early-deletion charges, retag job success each Jan 2.
- Revisit if HNS accounts gain index-tag lifecycle support, or if Cool/Cold reads exceed at-rest savings.
