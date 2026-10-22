# ADR-0013: Synthetic-only data; EIA/FRED adapters stay as unwired reference code

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ orchestrator)
- Related: SPEC-01, data-contracts §1.1, config-contract v1.2, T-102, T-103, T-105, T-108, T-117

## Context
The project owner cannot obtain EIA or FRED API keys (EIA registration is blocked). Gate G1 item 1 ("20+ years of
real daily data") and the `NEEDS-KEY` backfills therefore cannot run. The portfolio value of Tidal lies in the
platform: epoch Parquet, hot/cold tiering, the query path, backtests at scale and the FinOps numbers. None of
that depends on the vendor feed; it depends on a long, realistic, reproducible daily history with the awkward
features of energy markets (negative prices, contango/backwardation).

## Options
1. **Keep waiting for keys** — real data, but the gate and every downstream task that wants daily history stay
   blocked indefinitely. € impact: none, schedule risk: high.
2. **Synthetic-only, delete the adapters** — smallest tree, but loses tested, working client code that shows
   pagination, retry and raw-persistence patterns. € impact: none.
3. **Synthetic-only, keep the adapters as unwired reference code** — all data comes from the deterministic
   generators; `eia.py`, `fred.py`, `base.py` and their unit tests stay (the key is a constructor argument).
   € impact: none.

## Decision
Option 3. Every data need is met by deterministic synthetic data (seeded PCG64; same seed and frozen clock give
identical bytes).

- No `TIDAL_SOURCES__EIA_API_KEY` / `TIDAL_SOURCES__FRED_API_KEY` (config-contract v1.2). Because unknown
  `TIDAL_*` keys fail startup, a leftover variable is rejected loudly.
- No `tidal ingest backfill|daily` commands, no `tidal-ingest` entry point, no `scripts/verify_{eia,fred}.py`.
- New `tidal synth daily-history` generates the daily catalogue (spot, futures M1..M4, EUR/USD) as `1d` bars
  directly from 2005-01-01 (configurable) to the day before the clock, through the normal hot/cold router.
- **Instrument ids are kept** (`EIA.*`, `FRED.*`): renaming to `SYN.*.DAILY` would touch the API, storage paths,
  fixtures and every consumer for no functional gain. The `source` enum still reflects the id prefix; the truth
  is the `SYNTHETIC` quality bit (set on every row) and the "source series" column of data-contracts §1.1, which
  now names the series being emulated. This is a documented contract clarification (data-contracts changelog),
  not a schema change.

## Consequences
- SPEC-01 golden check "WTI 2020-04-20 stored negative (-36.98 / -37.63)" is replaced by a deterministic
  synthetic negative-price episode on the same date (spot -37.0, FUT1 -37.6, FUT2 positive), asserted in
  `tests/unit/ingest/test_ingest_synthetic_daily.py`. The real values are no longer asserted.
- Gate G1 item 1 is redefined: "20+ years of synthetic daily history (2005 onwards) ingested locally into
  Parquet + SQL Server container". The EIA discontinuation note for `FUT1..FUT4` (2024-04-05) no longer applies;
  synthetic futures run to the clock.
- T-108 (calibrate synthetic params to real EIA history) is superseded: the parameters stay judgemental and are
  labelled "stylised, not calibrated" in `synthetic_params.yaml`. No document may claim the data is calibrated or
  real.
- Do not interpret backtest results on this data as evidence of strategy quality; they demonstrate the platform.
- Restatements (`version+1`, `as_of`) are not produced by the daily history; they stay covered by the router and
  SQL repository tests, which write restated bars directly.
- Revisit if keys become available: wiring the adapters back needs a new config key (ADR + contract bump).
