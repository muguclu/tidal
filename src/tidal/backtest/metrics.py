"""Metric formulas (SPEC-05 §3, normative). Undefined values are None, never NaN/inf."""

import math

import numpy as np
from numpy.typing import NDArray

from tidal.contracts.models import BacktestMetrics

#: Periods per year by bar interval (252 trading days; intraday = 24h x 252 days).
MIN_OBS = 2
PERIODS_PER_YEAR: dict[str, int] = {"1d": 252, "1h": 252 * 24, "1m": 252 * 24 * 60}


def _fin(x: float) -> float | None:
    v = float(x)
    return v if math.isfinite(v) else None


def compute_metrics(
    equity: NDArray[np.float64],
    position: NDArray[np.float64],
    trade_pnls: NDArray[np.float64],
    n_trades: int,
    periods_per_year: int,
) -> BacktestMetrics:
    """Compute metrics from an equity curve and the per-position-segment P&L of closed trades.

    ``n_days`` in the cagr formula is the number of return observations (len(equity) - 1).
    """
    n = len(equity)
    turnover: float | None = None
    cagr = vol = sharpe = mdd = None
    if n >= MIN_OBS:
        n_periods = n - 1
        years = n_periods / periods_per_year
        with np.errstate(all="ignore"):
            ratio = equity[-1] / equity[0]
            cagr = _fin(ratio ** (periods_per_year / n_periods) - 1) if ratio > 0 else None
            r = np.diff(equity) / equity[:-1]
            r = r[np.isfinite(r)]
            peak = np.maximum.accumulate(equity)
            dd = equity / peak - 1
            mdd = _fin(dd.min()) if np.all(peak > 0) else None
        if len(r) >= MIN_OBS:
            sd = float(np.std(r, ddof=1))
            if sd > 0:
                vol = _fin(sd * math.sqrt(periods_per_year))
                sharpe = _fin(float(np.mean(r)) / sd * math.sqrt(periods_per_year))
        turnover = _fin(float(np.abs(np.diff(position, prepend=0.0)).sum()) / years)
    hit = _fin(float((trade_pnls > 0).sum()) / len(trade_pnls)) if len(trade_pnls) else None
    return BacktestMetrics(
        cagr=cagr,
        ann_vol=vol,
        sharpe=sharpe,
        max_drawdown=mdd,
        hit_rate=hit,
        turnover=turnover,
        n_trades=n_trades,
    )
