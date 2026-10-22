# SPEC-01 — Ingestion (real + synthetic)

Owner agent: `data-engineer` · Contracts: data-contracts §1–5, storage-layout §2, config-contract
Tasks: T-101 … T-106, T-117 (synthetic-only, ADR-0013)

## 1. Purpose
Generate 20+ years of synthetic daily energy/FX history and large, realistic synthetic
tick data (no vendor feed, ADR-0013), normalise both into `TICK_V1` / `BAR_V1`, validate, and write to the hot store (SQL) and
cold store (Parquet) according to age.

## 2. Components

| Module | Responsibility |
|---|---|
| `ingest/sources/base.py` | `Source` protocol: `fetch(instrument, start, end) -> Iterator[RawChunk]` |
| `ingest/sources/eia.py` | **Unwired reference code (ADR-0013).** EIA v2 API client: pagination, retry w/ exponential backoff + jitter, raw response saved to `raw/eia/{series}/{yyyymmdd}-{sha8}.json.gz` (sha8 = first 8 hex of the payload sha256, so restated responses never collide) |
| `ingest/sources/fred.py` | Unwired reference code: FRED client, same pattern |
| `ingest/sources/synthetic_daily.py` | Daily-history generator (§4.1), T-117 |
| `ingest/sources/synthetic.py` | Generator (§4) |
| `ingest/normalise.py` | Raw → arrow table in contract schema, UTC epoch ns |
| `ingest/validate.py` | Sets quality flags; rejects only schema violations (never business-valid oddities) |
| `ingest/router.py` | Splits rows by age: `ts ≥ clock.now() − hot_window` → SQL, else → Parquet (clock injected; `TIDAL_CLOCK__NOW` freezes it for synthetic demos) |
| `storage/aggregate.py` | Ticks → `bars_1s/1m/1h` (data-contracts §3.2), T-107 |
| `ingest/cli.py` | `tidal synth --scale L`, `tidal synth daily-history [--start 2005-01-01] [--seed N]` (no `tidal ingest` commands since ADR-0013) |

## 3. Behaviour
- **No real-source ingestion** (ADR-0013). The EIA/FRED clients (raw-first, resumable, restatement-aware) remain as
  tested reference code but nothing calls them. Router behaviour for restatements (`version+1`, `RESTATED`) is
  still covered by router/repository tests.
- `tidal synth` skips groups already present (resume); `tidal synth daily-history` is idempotent (unchanged cold
  bars are skipped, re-run writes 0 rows).
- Writes go through `storage.parquet_writer` and `storage.sql_repo` only.

## 4. Synthetic generator
- Per instrument: GBM with regime switching (2–3 Markov regimes: calm / stressed / crisis), Merton
  jumps, intraday seasonality (U-shaped activity), ticks arrive as a Poisson process with time-varying
  intensity. Power instrument allows negative prices (shifted process).
- Price level: GBM with a weak mean-reverting pull in log space (AR(1), half-life ≈ 350 business days) so
  20-year paths stay plausible; power uses a shifted process (`exp(x) − offset`).
- Parameters in `ingest/sources/synthetic_params.yaml`. **v1 targets are judgemental** (order of magnitude of
  commonly quoted daily vols) and the generator is verified to hit its own targets within ±10 %.
  **T-108 (calibration to real vol) is superseded by ADR-0013**: the parameters stay judgemental and must be described as stylised, never as calibrated.
- Injects 0.01 % outliers and 0.1 % gaps (documented, flagged `SYNTHETIC`).
- Date range from `TIDAL_SYNTH__START/END` (default 2005-01-01 … 2025-12-31). The range is part of the
  determinism key. Because synthetic data ends before "today", hot-window demos run with
  `TIDAL_CLOCK__NOW` set inside the range (e.g. `2025-12-31T23:59:59Z`).
- **Deterministic:** `numpy.random.Generator(PCG64(seed))`, one child stream per (instrument, year) via
  `SeedSequence.spawn` ⇒ years can be generated **in parallel** and still be reproducible.
- Streams output in monthly chunks; memory bounded: peak RSS < 2 GB **for the whole run** at L scale (the
  ingest CLI caps worker count accordingly).

### 4.1 Synthetic daily history (T-117)
- `tidal synth daily-history` writes `1d` bars (stamped at UTC midnight of the trade date, ADR-0009; Monday-Friday)
  for the 12 daily catalogue ids (`EIA.BRENT.SPOT`, `EIA.WTI.SPOT/FUT1-4`, `EIA.HH.SPOT/FUT1-4`,
  `FRED.EURUSD.FX`) from `--start` (default `TIDAL_SYNTH__START`) to the day before the clock. Ids are kept, values
  are synthetic and flagged `SYNTHETIC`. No ticks, so the run takes seconds.
- Spot: OU deviation around a stylised anchor curve, Student-t(5) shocks, stress-window vol multipliers, Brent/WTI
  correlated. Futures: carry falls (towards backwardation) as price rises, plus seasonality (Henry Hub) and basis
  noise, so M1-M2 spreads change sign. Deterministic episode: WTI spot -37.0 / FUT1 -37.6 on 2020-04-20.
- Parameters in `synthetic_params.yaml` (`daily_history`); same PCG64 stream scheme as §4; a bar never depends on the
  requested range.

## 5. Acceptance criteria
- [ ] `tidal synth daily-history` produces 2005-01-03 … clock-1 daily bars for all 12 series, schema-conformant,
      byte-identical across runs with the same seed and `TIDAL_CLOCK__NOW`; re-running writes nothing new.
- [ ] Synthetic WTI negative-price episode on 2020-04-20 stored with the `NEGATIVE` flag (golden test, replaces the
      real EIA print: spot -37.0, FUT1 -37.6, FUT2 positive).
- [ ] Term structure has both contango and backwardation days for WTI and Henry Hub futures.
- [ ] `tidal synth --scale S --seed 1` twice with the same `TIDAL_CLOCK__NOW` ⇒ identical file hashes (the clock
      sets `ingested_at_ns` and the hot/cold split); re-running into the same store writes nothing new.
- [ ] Generating L scale on 8 cores finishes < 60 min locally; peak RSS < 2 GB.
- [ ] Re-running `daily-history` with the same inputs writes 0 rows.
- [ ] (Router level) a restated bar produces version 2 and `as_of` returns version 1 for earlier knowledge time.
- [ ] Unit test coverage ≥ 90 % for `ingest/`.
