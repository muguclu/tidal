"""Strategy signal functions (SPEC-05 §2).

Each function is pure: ``signals(bars, params) -> position series`` in [-1, 1], aligned to
``bars`` rows. The value at row ``t`` uses only rows ``<= t`` (the decision is taken at the
close of ``t``); the engine, not the strategy, delays execution to ``t+1``.
``bars`` needs ``ts_ns`` (sorted ascending) and ``close``. Negative prices are valid input.
"""

import polars as pl

from tidal.contracts.models import MaCrossoverParams, MeanReversionParams


def ma_crossover_signals(bars: pl.DataFrame, params: MaCrossoverParams) -> pl.Series:
    """+1 when the fast SMA is above the slow SMA, -1 when below, 0 in warm-up or on ties."""
    close = pl.col("close")
    fast = close.rolling_mean(params.fast, min_samples=params.fast)
    slow = close.rolling_mean(params.slow, min_samples=params.slow)
    sig = (
        pl.when(fast.is_null() | slow.is_null())
        .then(0.0)
        .otherwise((fast - slow).sign())
        .alias("position")
    )
    return bars.select(sig).to_series()


def mean_reversion_signals(bars: pl.DataFrame, params: MeanReversionParams) -> pl.Series:
    """Rolling z-score of the level with entry/exit hysteresis.

    z = (x - mean_L) / std_L (ddof=1, window ending at t). Long when z < -z_entry, short when
    z > z_entry, flat when |z| <= z_exit, otherwise hold. Zero-variance windows hold the
    previous position (z undefined). Warm-up is flat.
    """
    close = pl.col("close")
    mean = close.rolling_mean(params.lookback, min_samples=params.lookback)
    std = close.rolling_std(params.lookback, min_samples=params.lookback, ddof=1)
    z = pl.when(std > 0).then((close - mean) / std).otherwise(None)
    event = (
        pl.when(z < -params.z_entry)
        .then(1.0)
        .when(z > params.z_entry)
        .then(-1.0)
        .when(z.abs() <= params.z_exit)
        .then(0.0)
        .otherwise(None)
    )
    # forward fill is a computation of position state here, not a storage fill.
    return bars.select(event.forward_fill().fill_null(0.0).alias("position")).to_series()


def calendar_spread_signals(bars: pl.DataFrame, params: MeanReversionParams) -> pl.Series:
    """Mean reversion on the spread; ``bars.close`` must already be ``leg_a - leg_b``."""
    return mean_reversion_signals(bars, params)
