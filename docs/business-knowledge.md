# Business Knowledge — Front-Office Trading Data

Read this before making any decision about data semantics. When code and this document disagree,
open an issue — do not silently pick one.

## 1. Who the users are

| Persona | What they do | What they need from Tidal | Role in Tidal |
|---|---|---|---|
| **Trader** | Takes positions in physical & financial energy products | Fast access to recent prices, spreads, simple history charts | `Tidal.Trader` (read hot + cold) |
| **Quant / Analyst** | Builds and validates models and strategies | Full 20+ yr history, reproducible backtests, bulk export | `Tidal.Quant` (read + submit backtests) |
| **Risk / Middle office** | Daily VaR, limits, P&L explain | End-of-day settled prices, as-of snapshots, nightly feed | `Tidal.Reader` + legacy CSV feed |
| **Platform owner** | Runs the platform | Cost visibility, health, audit | `Tidal.Admin` |

Front office values **latency for recent data** and **completeness for history**. These two
needs drive the hot/cold split: the last ~90 days are queried constantly, the older 20 years are
scanned in bulk occasionally (model training, backtests, regulatory look-backs).

## 2. Market data basics

- **Spot price**: price for (near-)immediate delivery. EIA publishes daily spot for Brent, WTI,
  Henry Hub.
- **Futures / forward**: price today for delivery in a future period (e.g. "Dec-27 Brent"). A
  **forward curve** is the set of prices across delivery periods (tenors) on one date.
- **Contango**: later tenors priced higher than nearer ones. **Backwardation**: the opposite.
- **Calendar spread**: price difference between two tenors (e.g. M1–M2). A classic strategy input.
- **Roll**: when the front contract expires, a continuous series switches to the next contract.
  Continuous series must record the roll method (`ratio`, `difference`, `none`).
- **Settlement price**: official end-of-day price used for margining and P&L. Risk uses
  settlements, not last trade.
- **OHLCV bar**: open, high, low, close, volume over an interval (1s, 1m, 1h, 1d).
- **Tick**: a single price observation (trade or quote) with its timestamp.
- **As-of query**: "what did we *know* at time T?" — requires keeping `ingested_at` separately from
  the market timestamp `ts_ns`, because data gets corrected/restated.

## 3. Data quality realities (design for these)

| Reality | Example | Rule in Tidal |
|---|---|---|
| Negative prices | WTI front-month settled at −$37.63 on 2020-04-20 | Never clamp or reject negatives; validator sets the `NEGATIVE` quality flag, keeps value |
| Missing days | Holidays, exchange closures, source gaps | No forward-fill in storage; fill only in query layer if requested (`fill=ffill`) |
| Restatements | Source corrects a past value | Append new version with later `ingested_at`; latest wins unless `as_of` given |
| Time zones / DST | Exchange local time vs UTC | Store UTC epoch ns; keep `exchange_tz` in instrument metadata |
| Unit differences | $/bbl vs $/MMBtu vs €/MWh | `unit` and `currency` are mandatory instrument metadata |
| Outliers | Fat-finger ticks in synthetic data | Synthetic generator injects them deliberately (0.01 %) so validators are exercised |

## 4. Data sources (free/public)

| Source | Series | History | Notes |
|---|---|---|---|
| EIA Open Data API v2 | Brent spot (RBRTE), WTI spot (RWTC), Henry Hub (RNGWHHD) | WTI 1986→, Brent 1987→, HH 1997→ | Free API key; daily |
| EIA Open Data API v2 | WTI futures contract 1–4 (RCLC1–4), Henry Hub futures 1–4 (RNGC1–4) | WTI 1983→, HH 1994→ | Nearby series, no roll adjustment; enable M1–M2 calendar spreads |
| FRED | EUR/USD (DEXUSEU), other macro | 1999→ | Free API key |
| Synthetic generator | ~10 instruments, ticks/1s bars | Configurable, default 2005–2025 | GBM + jumps + regime switching, calibrated to real daily vol; seeded |

Licensed exchange data (ICE, CME, EEX) is out of scope; mention in README that the architecture is
source-agnostic.

## 5. Why 20+ years matters

- Strategy validation across regimes: 2008 crash, 2014–16 oil collapse, 2020 negative WTI,
  2022 European gas crisis.
- Model training needs long history; risk look-backs and stress scenarios replay old periods.
- Data volume makes naive storage expensive → the business case for epoch Parquet + tiering.

## 6. Backtesting semantics (deterministic)

- Input: instrument(s), date range, strategy name + params, bar interval, cost model
  (fee bps, slippage bps).
- Signals computed on bar close `t`, executed at open `t+1` (no look-ahead). Enforced by tests.
- Outputs: equity curve, trades, metrics (CAGR, annualised vol, Sharpe with rf=0, max drawdown,
  hit rate, turnover). Formulas are fixed in `docs/specs/SPEC-05-backtest.md`.
- Same input + same `seed` ⇒ byte-identical result file (hash checked in tests).

## 7. Access-pattern assumptions (used by FinOps)

| Pattern | Frequency | Data age | Target tier |
|---|---|---|---|
| Trader dashboards, latest prices | Many/min | < 7 days | Azure SQL (hot) |
| Intraday analysis | Many/day | < 90 days | Azure SQL (hot) |
| Quant research scans | Few/day | 90 d – 3 yrs | Blob **Hot** Parquet |
| Backtests, model training | Few/week | 3 – 10 yrs | Blob **Cool** |
| Regulatory look-back, rare replays | Few/year | 10+ yrs | Blob **Cold** |
| Raw source responses (audit) | Almost never | any | Blob **Archive** |

## 8. Glossary

`bbl` barrel · `MMBtu` million British thermal units · `M1/M2` first/second month contract ·
`EOD` end of day · `VaR` value at risk · `P&L` profit and loss · `bps` basis points (0.01 %) ·
`tenor` delivery period · `as-of` knowledge-time view · `hot window` data kept in SQL (default 90 d).
