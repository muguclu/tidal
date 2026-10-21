# ADR-0004: Epoch Parquet, tiering by data age, no Archive for market data
- Status: Proposed · Related: SPEC-02, SPEC-03, storage-layout
## Context
20+ years of ticks are large; access frequency drops sharply with data age (business-knowledge §7).
Backfills write old data "today", so last-modified-based lifecycle rules would keep old data Hot.
## Options
1. Lifecycle by last-modified (simple, wrong for backfills).
2. Lifecycle by blob index tag `age_band` set from data year (correct, needs flat namespace + retag job).
3. Separate containers per tier (manual moves, more code).
## Decision
Option 2. Archive excluded for `md/` because rehydration latency (hours) breaks backtests.
## Consequences
Flat namespace (no ADLS Gen2 HNS features). Yearly retag job. Early-deletion charges modelled.
Re-check tag support for HNS accounts before revisiting.
