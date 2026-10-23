"""Deterministic backtest engine (SPEC-05 §2-3). Pure over polars frames: no I/O, no clock."""

from tidal.backtest.engine import BacktestOutput, run_backtest
from tidal.backtest.metrics import compute_metrics
from tidal.backtest.strategies import (
    calendar_spread_signals,
    ma_crossover_signals,
    mean_reversion_signals,
)

__all__ = [
    "BacktestOutput",
    "calendar_spread_signals",
    "compute_metrics",
    "ma_crossover_signals",
    "mean_reversion_signals",
    "run_backtest",
]
