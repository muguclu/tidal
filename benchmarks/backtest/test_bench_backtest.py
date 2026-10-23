"""SPEC-05 §5 perf budget: 26 years of daily bars, ma_crossover, < 2 s (pytest-benchmark)."""

import numpy as np
import polars as pl
from pytest_benchmark.fixture import BenchmarkFixture

from tidal.backtest import run_backtest
from tidal.contracts.models import BacktestRequest

DAY = 86_400 * 10**9
T0 = 1_577_836_800 * 10**9
N = 26 * 365


def test_bench_ma_crossover_26y_daily(benchmark: BenchmarkFixture) -> None:
    px = (np.cumsum(np.random.default_rng(3).normal(size=N)) + 500).tolist()
    bars = pl.DataFrame({"ts_ns": [T0 + i * DAY for i in range(N)], "open": px, "close": px})
    request = BacktestRequest.model_validate(
        {
            "strategy": "ma_crossover",
            "instrument_ids": ["EIA.BRENT.SPOT"],
            "interval": "1d",
            "start": T0,
            "end": T0 + (N + 1) * DAY,
            "params": {"fast": 20, "slow": 100},
            "costs": {"fee_bps": 1.0, "slippage_bps": 2.0},
            "seed": 42,
        }
    ).root
    benchmark(run_backtest, request, {"EIA.BRENT.SPOT": bars})
    assert benchmark.stats is not None
    assert benchmark.stats["max"] < 2.0
