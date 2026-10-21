# ADR-0009: Daily bars are stamped at UTC midnight of the trade date

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ orchestrator, from the T-101 review)
- Related: data-contracts §3, storage-layout §2, SPEC-01, SPEC-03 §2, T-101, T-105

## Context
data-contracts §3 says `ts_ns` is the bar open time in UTC but does not say which midnight a `1d` bar for a
local trade date uses. EIA and FRED publish dates without a time zone. Exchange-local midnight would put a
London 1 July bar at 30 June 23:00 UTC: wrong month partition, `floor_to_interval("1d")` off by hours, and
hot-window/as-of edges shifted by the exchange offset.

## Options
1. **UTC midnight of the trade date.** Partitions, flooring and joins across instruments line up; no DST
   handling for daily data. Con: not the true exchange session open. € impact: none.
2. **Exchange-local midnight converted to UTC.** Closer to "session open". Con: daily bars of one date fall
   into different UTC days per exchange, month partitions split at month ends, DST dependence. € impact: none.

## Decision
Option 1: `1d` bars use UTC midnight of the trade date; intraday bars and ticks stay true UTC instants.

## Consequences
Gap detection for `1d` bars uses the UTC date. Cross-instrument spreads (e.g. WTI FUT1−FUT2, Brent−WTI) join on
the same `ts_ns`. As-of semantics are unaffected (they use `ingested_at_ns`). Revisit only if a source publishes
true session timestamps for daily settlements. This is a v1.1 clarification (no data written yet), so no
version bump of `BAR_V1`.
