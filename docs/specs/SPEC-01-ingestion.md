# SPEC-01 — Ingestion (real + synthetic)

Owner agent: `data-engineer` · Contracts: data-contracts §1–5, storage-layout §2, config-contract
Tasks: T-101 … T-106

## 1. Purpose
Load 20+ years of daily energy/FX history from EIA & FRED and generate large, realistic synthetic
tick data, normalise both into `TICK_V1` / `BAR_V1`, validate, and write to the hot store (SQL) and
cold store (Parquet) according to age.

## 2. Components

| Module | Responsibility |
|---|---|
| `ingest/sources/base.py` | `Source` protocol: `fetch(instrument, start, end) -> Iterator[RawChunk]` |
| `ingest/sources/eia.py` | EIA v2 API client: pagination, retry w/ exponential backoff + jitter, raw response saved to `raw/eia/{series}/{yyyymmdd}-{sha8}.json.gz` (sha8 = first 8 hex of the payload sha256, so restated responses never collide) |
| `ingest/sources/fred.py` | FRED client, same pattern |
| `ingest/sources/synthetic.py` | Generator (§4) |
| `ingest/normalise.py` | Raw → arrow table in contract schema, UTC epoch ns |
| `ingest/validate.py` | Sets quality flags; rejects only schema violations (never business-valid oddities) |
| `ingest/router.py` | Splits rows by age: `ts ≥ clock.now() − hot_window` → SQL, else → Parquet (clock injected; `TIDAL_CLOCK__NOW` freezes it for synthetic demos) |
| `storage/aggregate.py` | Ticks → `bars_1s/1m/1h` (data-contracts §3.2), T-107 |
| `ingest/cli.py` | `tidal ingest backfill --source eia --from 1986-01-01`, `tidal ingest daily`, `tidal synth --scale L` |

## 3. Behaviour
- **Backfill** is resumable: progress checkpoint in `ops.ingest_checkpoint` (source, instrument, last_ts_ns).
- **Daily** run (Container Apps Job, cron `15 22 * * 1-5` UTC) fetches last 10 days to catch restatements; rows
  identical to stored are skipped, changed rows written as `version+1` with `RESTATED` flag.
- Raw responses are stored before normalisation (audit + replay without hitting the API).
- Writes go through `storage.parquet_writer` and `storage.sql_repo` only.

## 4. Synthetic generator
- Per instrument: GBM with regime switching (2–3 Markov regimes: calm / stressed / crisis), Merton
  jumps, intraday seasonality (U-shaped activity), ticks arrive as a Poisson process with time-varying
  intensity. Power instrument allows negative prices (shifted process).
- Price level: GBM with a weak mean-reverting pull in log space (AR(1), half-life ≈ 350 business days) so
  20-year paths stay plausible; power uses a shifted process (`exp(x) − offset`).
- Parameters in `ingest/sources/synthetic_params.yaml`. **v1 targets are judgemental** (order of magnitude of
  commonly quoted daily vols) and the generator is verified to hit its own targets within ±10 %.
  **Calibration to real Brent/HH realised vol per regime (±10 %) is T-108**, after EIA ingest (T-105) exists.
- Injects 0.01 % outliers and 0.1 % gaps (documented, flagged `SYNTHETIC`).
- Date range from `TIDAL_SYNTH__START/END` (default 2005-01-01 … 2025-12-31). The range is part of the
  determinism key. Because synthetic data ends before "today", hot-window demos run with
  `TIDAL_CLOCK__NOW` set inside the range (e.g. `2025-12-31T23:59:59Z`).
- **Deterministic:** `numpy.random.Generator(PCG64(seed))`, one child stream per (instrument, year) via
  `SeedSequence.spawn` ⇒ years can be generated **in parallel** and still be reproducible.
- Streams output in monthly chunks; memory bounded: peak RSS < 2 GB **for the whole run** at L scale (the
  ingest CLI caps worker count accordingly).

## 5. Acceptance criteria
- [ ] Backfill EIA Brent, WTI, Henry Hub and FRED EUR/USD from first available date; row counts logged
      and asserted against source totals.
- [ ] WTI 2020-04-20 stored as negative with `NEGATIVE` flag (golden test): `EIA.WTI.FUT1` −37.63, `EIA.WTI.SPOT` −36.98.
- [ ] `tidal synth --scale S --seed 1` twice with the same `TIDAL_CLOCK__NOW` ⇒ identical file hashes (the clock
      sets `ingested_at_ns` and the hot/cold split); re-running into the same store writes nothing new.
- [ ] Generating L scale on 8 cores finishes < 60 min locally; peak RSS < 2 GB.
- [ ] Re-running daily ingest with unchanged source writes 0 rows.
- [ ] Restated value produces version 2 and `as_of` query returns version 1 for earlier knowledge time.
- [ ] Unit test coverage ≥ 90 % for `ingest/`.
