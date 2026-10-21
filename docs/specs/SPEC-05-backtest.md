# SPEC-05 — Backtest Engine & Worker

Owner agent: `data-engineer` (engine) + `platform-engineer` (worker runtime) · Contracts:
messaging-contract, api-contract `BacktestRequest/Result`, data-contracts §4 · Tasks: T-301 … T-304

## 1. Purpose
Run deterministic strategy backtests over 20+ years of data, fanned out as many independent jobs on AKS.
The "model running over 20+ years of data" requirement in a form that is easy to demo and measure.

## 2. Engine (`backtest/`)
- Vectorised with polars; no Python loops over bars.
- Strategies (pure functions `signals(bars, params) -> position series ∈ [-1, 1]`):
  - `ma_crossover(fast, slow)` · `mean_reversion(lookback, z_entry, z_exit)` ·
    `calendar_spread(lookback, z_entry, z_exit)` on the spread `instrument_ids[0] − instrument_ids[1]`
    (e.g. `EIA.WTI.FUT1 − EIA.WTI.FUT2`, data-contracts §1.1).
- Execution: position decided at close `t`, filled at open `t+1` (daily EIA bars: next close, documented).
- Costs: `fee_bps` and `slippage_bps` applied to traded notional.

## 3. Metric formulas (normative)
Let daily returns `r_t`, N = trading days per year = 252.
- `cagr = (equity_end / equity_start)^(N / n_days) − 1`
- `ann_vol = std(r_t, ddof=1) · √N`
- `sharpe = mean(r_t) / std(r_t, ddof=1) · √N` (rf = 0)
- `max_drawdown = min_t(equity_t / max_{s≤t} equity_s − 1)`
- `hit_rate = winning trades / closed trades` · `turnover = Σ|Δposition| / years`
Golden tests with hand-computed fixtures (10-bar series) lock these.

## 4. Worker (`worker/`)
- Runs as a **KEDA ScaledJob** (one pod per message, `maxReplicaCount 50`, `pollingInterval 15`,
  scaler `azure-servicebus` on `backtest-jobs`, auth via **workload identity**).
- Flow: receive 1 message → check `_SUCCESS` → load data via unified reader → run → write
  `equity.parquet`, `trades.parquet`, `metrics.json`, `_SUCCESS` → send `backtest.result/v1` → complete.
- Lock renewal with `AutoLockRenewer`; SIGTERM (spot eviction) → abandon message, exit 143.
- Pod: requests 1 vCPU / 2 GiB, limits 2 vCPU / 4 GiB; tolerates `kubernetes.azure.com/scalesetpriority=spot`.
- Result aggregator in API marks parent `succeeded` when all children done, `partial` if any failed.

## 5. Acceptance criteria
- [ ] Look-ahead test: shifting future data does not change past signals.
- [ ] Same request + seed ⇒ identical `result_sha256` across runs and machines (CI matrix).
- [ ] 26 yrs daily Brent `ma_crossover` < 2 s; 10 yrs 1-minute synthetic < 60 s per pod.
- [ ] 100-job sweep on AKS: scale 0 → ≥ 20 pods → 0, total wall time and € recorded (SPEC-09).
      Needs PAYG + vCPU quota (PLANNING §7); if quota is refused, record the max reached and model the rest.
- [ ] Locally (kind/k3d) the same 0 → N → 0 is demonstrated with a 20-job sweep (LOCAL-DEV §1).
- [ ] Forced pod deletion mid-job ⇒ message redelivered, job completes once (no duplicate result rows).
