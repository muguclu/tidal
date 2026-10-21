# API Contract — v1

Base path `/v1`. JSON over HTTPS. Machine-readable source: `docs/contracts/openapi.yaml`.
CI fails if FastAPI's generated OpenAPI differs from it (`oasdiff breaking`).

## 1. Conventions

- Auth: `Authorization: Bearer <Entra ID access token>`; audience `api://tidal`; roles claim.
- Time parameters accept ISO-8601 UTC (`2020-04-20T00:00:00Z`) **or** epoch ns integers.
  Responses always return `ts` as ISO-8601 **and** `ts_ns` as integer.
- Pagination: cursor-based (`next_cursor`), `limit` default 10 000, max 100 000 rows — for
  `format=json` only. `arrow`/`parquet` stream the whole range (bounded by the range limits in SPEC-04 §3).
- Large results: `format=json|arrow|parquet` (`application/vnd.apache.arrow.stream` for arrow).
- Errors: RFC 7807 `application/problem+json` with `type`, `title`, `status`, `detail`,
  `correlation_id`.
- Every response carries `X-Correlation-Id`. Every successful (2xx) market-data response (`/bars`,
  `/ticks`, `/latest`, `/spreads`) also carries `X-Tidal-Tier` (`hot`, `cold`, `mixed`) — used in FinOps to attribute
  query cost to tiers.

### 1.1 Roles (Entra app roles, cumulative)
`Tidal.Reader` ⊂ `Tidal.Trader` ⊂ `Tidal.Quant`; `Tidal.Admin` is separate and grants only `/admin/*`
(an operator also given Quant gets both). The **Role** column below is the minimum role.

| Endpoint group | Reader | Trader | Quant | Admin |
|---|---|---|---|---|
| health | ✓ (no auth) | ✓ | ✓ | ✓ |
| instruments, bars, latest | ✓ | ✓ | ✓ | — |
| ticks, spreads | — | ✓ | ✓ | — |
| backtests (submit/read own) | — | — | ✓ | — |
| admin | — | — | — | ✓ |

## 2. Endpoints

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/v1/health/live` | none | liveness |
| GET | `/v1/health/ready` | none | checks SQL, Blob, Service Bus reachability |
| GET | `/v1/instruments` | Reader | list instruments (filter `asset_class`, `source`) |
| GET | `/v1/instruments/{id}` | Reader | one instrument |
| GET | `/v1/bars` | Reader | `instrument_id`, `interval`, `start`, `end`, `fill?`, `as_of?`, `format?` |
| GET | `/v1/ticks` | Trader | `instrument_id`, `start`, `end` (max 31 days per call), `format?` |
| GET | `/v1/latest` | Reader | last bar per instrument list (hot only) |
| GET | `/v1/spreads` | Trader | `leg_a`, `leg_b`, `interval`, `start`, `end` → a−b series |
| POST | `/v1/backtests` | Quant | submit `BacktestRequest` → `202` + `job_id` |
| GET | `/v1/backtests/{job_id}` | Quant | status + `BacktestResult` when done |
| GET | `/v1/backtests` | Quant | list own jobs |
| POST | `/v1/admin/tiering/run` | Admin | trigger migration job (enqueues) |
| GET | `/v1/admin/storage/stats` | Admin | rows/bytes per tier, from last stats run |

## 3. Schemas

```jsonc
// Bar — EIA.WTI.FUT1 (NYMEX contract 1) settled at −37.63 on 2020-04-20; EIA WTI spot (RWTC) printed −36.98
{ "ts": "2020-04-20T00:00:00Z", "ts_ns": 1587340800000000000,
  "open": -37.63, "high": -37.63, "low": -37.63, "close": -37.63,
  "volume": null, "quality": 1, "version": 1 }

// BarsResponse
{ "instrument_id": "EIA.WTI.FUT1", "interval": "1d", "tier": "cold",
  "rows": [Bar, ...], "next_cursor": null }

// BacktestRequest
{ "strategy": "ma_crossover",              // enum: ma_crossover | mean_reversion | calendar_spread
  "instrument_ids": ["EIA.BRENT.SPOT"],
  "interval": "1d", "start": "2000-01-01T00:00:00Z", "end": "2025-12-31T00:00:00Z",
  "params": { "fast": 20, "slow": 100 },
  "costs": { "fee_bps": 1.0, "slippage_bps": 2.0 },
  "seed": 42,
  "sweep": { "fast": [10, 20, 50], "slow": [100, 200] }   // optional → fan-out N child jobs
}
// BacktestRequest is a union discriminated by `strategy` (OpenAPI oneOf; pydantic discriminated union):
//   ma_crossover    → 1 instrument; params { fast:int≥1, slow:int≥1 }
//   mean_reversion  → 1 instrument; params { lookback:int≥2, z_entry>0, z_exit≥0 }
//   calendar_spread → 2 instruments [leg_a, leg_b] (e.g. EIA.WTI.FUT1, EIA.WTI.FUT2); params as mean_reversion
// sweep: ≥ 1 key (param names of the strategy), each list ≥ 1 value, cartesian product ≤ 200 (else 413).
// interval ∈ 1m|1h|1d (1s excluded: a 20-year 1s job exceeds the per-pod budget, SPEC-05 §5).
// seed: strategies v1 are fully deterministic; seed is recorded and hashed for future stochastic
// strategies (e.g. bootstrap), so the result hash already covers it.

// BacktestStatus
{ "job_id": "bt_01J...", "status": "queued|running|succeeded|failed|partial",
  "children": { "total": 6, "succeeded": 4, "failed": 0 },
  "submitted_at": "...", "finished_at": null, "result": BacktestResult | null }

// BacktestResult
{ "metrics": { "cagr": 0.041, "ann_vol": 0.31, "sharpe": 0.13, "max_drawdown": -0.62,
               "hit_rate": 0.48, "turnover": 3.2, "n_trades": 211 },
  "equity_url": "backtests/bt_01J.../equity.parquet",
  "trades_url": "backtests/bt_01J.../trades.parquet",
  "result_sha256": "…" }
// Metrics are nullable: a value with no mathematical definition is null, never NaN/inf/error —
// e.g. hit_rate with 0 closed trades, sharpe/ann_vol with < 2 returns or zero volatility.
// job_id = "bt_" + ULID (Crockford base32, 26 chars, first char 0–7). Time fields accept ISO-8601 UTC
// or an epoch-ns integer string (§1); serialised as ts_ns.
```

## 4. Status codes

`200` ok · `202` accepted (backtests, tiering run) · `400` validation (FastAPI's default 422 is
mapped to 400 problem+json) · `401` no/invalid token · `403` role
missing · `404` unknown instrument/job, or a job owned by another principal (no existence leak) ·
`413` range too large, or sweep expands to > 200 children · `429` rate limited · `503` dependency down.
