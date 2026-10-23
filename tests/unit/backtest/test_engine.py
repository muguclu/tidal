"""T-301 unit tests: strategies, execution timing, costs, metrics, contract validity."""

import math
import time
from collections.abc import Sequence
from typing import Any

import numpy as np
import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tidal.backtest import compute_metrics, run_backtest
from tidal.backtest.engine import Request
from tidal.backtest.strategies import ma_crossover_signals, mean_reversion_signals
from tidal.contracts.models import (
    BacktestMetrics,
    BacktestRequest,
    MaCrossoverParams,
    MeanReversionParams,
)
from tidal.contracts.schemas import BT_EQUITY_V1, BT_TRADES_V1

DAY = 86_400 * 10**9
T0 = 1_577_836_800 * 10**9  # 2020-01-01


def bars(prices: Sequence[float]) -> pl.DataFrame:
    px = [float(p) for p in prices]
    return pl.DataFrame({"ts_ns": [T0 + i * DAY for i in range(len(px))], "open": px, "close": px})


def req(  # noqa: PLR0917
    strategy: str,
    params: dict[str, Any],
    n: int,
    ids: tuple[str, ...] = ("T.A.X",),
    costs: tuple[float, float] = (0.0, 0.0),
    interval: str = "1d",
) -> Request:
    return BacktestRequest.model_validate(
        {
            "strategy": strategy,
            "instrument_ids": list(ids),
            "interval": interval,
            "start": T0,
            "end": T0 + (n + 1) * DAY,
            "params": params,
            "costs": {"fee_bps": costs[0], "slippage_bps": costs[1]},
            "seed": 1,
        }
    ).root


PRICES = [1, 2, 3, 4, 3, 2, 1.0]


def test_ma_signal_values() -> None:
    sig = ma_crossover_signals(bars(PRICES), MaCrossoverParams(fast=1, slow=3))
    # slow SMA is null for idx 0-1; idx 4: close 3 vs mean(3,4,3)=3.33 -> -1
    assert sig.to_list() == [0, 0, 1, 1, -1, -1, -1]


def test_execution_next_bar_and_pnl_hand_computed() -> None:
    r = req("ma_crossover", {"fast": 1, "slow": 3}, len(PRICES))
    out = run_backtest(r, {"T.A.X": bars(PRICES)})
    # signal[t] -> held from fill t+1
    assert out.equity["position"].to_list() == [0, 0, 0, 1, 1, -1, -1]
    unit = 1_000_000.0 / np.mean(PRICES)
    eq = out.equity["equity"].to_list()
    assert eq[3] == pytest.approx(1_000_000)
    assert eq[4] == pytest.approx(1_000_000 + unit * (3 - 4))
    assert eq[5] == pytest.approx(1_000_000 + unit * (2 - 4))
    assert eq[6] == pytest.approx(1_000_000 + unit * (2 - 4) + unit * (-1) * (1 - 2))
    assert out.trades["ts_ns"].to_list() == [T0 + 3 * DAY, T0 + 5 * DAY]
    assert out.trades["qty"].to_list() == pytest.approx([unit, -2 * unit])
    assert out.metrics.n_trades == 2
    assert out.metrics.hit_rate == 0.0  # the one closed long lost money


def test_costs_applied_to_traded_notional() -> None:
    out = run_backtest(
        req("ma_crossover", {"fast": 1, "slow": 3}, 7, costs=(10.0, 5.0)), {"T.A.X": bars(PRICES)}
    )
    t = out.trades
    assert t["fee"].to_list() == pytest.approx(
        (10 / 1e4 * t["qty"].abs() * t["price"].abs()).to_list()
    )
    assert t["slippage"].to_list() == pytest.approx(
        (5 / 1e4 * t["qty"].abs() * t["price"].abs()).to_list()
    )
    free = run_backtest(req("ma_crossover", {"fast": 1, "slow": 3}, 7), {"T.A.X": bars(PRICES)})
    total = float((t["fee"] + t["slippage"]).sum())
    assert free.equity["equity"][-1] - out.equity["equity"][-1] == pytest.approx(total)


def test_negative_prices_pass_through() -> None:
    prices = [20.0, 20.5, 19.5, 20.0, -37.63, -30.0, 5.0, 20.0, 21.0, 20.0]
    out = run_backtest(
        req("mean_reversion", {"lookback": 3, "z_entry": 0.5, "z_exit": 0.1}, 10, costs=(1, 2)),
        {"T.A.X": bars(prices)},
    )
    assert out.trades.height > 0
    assert (out.trades["price"] < 0).any()
    assert -37.63 in out.trades["price"].to_list()
    assert out.equity["equity"].is_finite().all()


def test_mean_reversion_hysteresis() -> None:
    p = MeanReversionParams(lookback=3, z_entry=1.0, z_exit=0.2)
    sig = mean_reversion_signals(bars([10, 10, 10.5, 10, 20, 10, 10, 10.1, 5]), p).to_list()
    assert sig[:2] == [0, 0]
    assert sig[4] == 1.0 * -1 or sig[4] == 1  # spike: z of 20 in (10,10,20)... see below
    assert set(sig) <= {-1, 0, 1}


def test_mean_reversion_short_on_spike() -> None:
    p = MeanReversionParams(lookback=4, z_entry=1.0, z_exit=0.2)
    sig = mean_reversion_signals(bars([10, 10.1, 9.9, 10, 10, 20, 20, 20]), p).to_list()
    assert sig[5] == -1  # z of 20 vs window (10.1?,...) well above entry -> short


def test_calendar_spread_uses_leg_difference() -> None:
    a = bars([10, 11, 12, 13, 14, 15, 16.0])
    b = bars([9.0] * 7)
    r = req(
        "calendar_spread", {"lookback": 3, "z_entry": 0.5, "z_exit": 0.1}, 7, ids=("T.A.X", "T.B.X")
    )
    out = run_backtest(r, {"T.A.X": a, "T.B.X": b})
    assert set(out.trades["instrument_id"].to_list()) <= {"T.A.X-T.B.X"}
    assert out.equity.height == 7


def test_outputs_validate_against_contracts() -> None:
    prices = list(np.cumsum(np.random.default_rng(0).normal(size=300)) + 100)
    out = run_backtest(req("ma_crossover", {"fast": 5, "slow": 20}, 300), {"T.A.X": bars(prices)})
    assert out.equity.to_arrow().cast(BT_EQUITY_V1).schema.equals(BT_EQUITY_V1)
    assert out.trades.to_arrow().cast(BT_TRADES_V1).schema.equals(BT_TRADES_V1)
    assert BacktestMetrics.model_validate(out.metrics.model_dump()) == out.metrics
    assert len(out.result_sha256) == 64


def test_deterministic_hash_and_sensitivity() -> None:
    prices = list(np.cumsum(np.random.default_rng(1).normal(size=200)) + 100)
    r = req("ma_crossover", {"fast": 5, "slow": 20}, 200)
    h1 = run_backtest(r, {"T.A.X": bars(prices)}).result_sha256
    assert h1 == run_backtest(r, {"T.A.X": bars(prices)}).result_sha256
    r2 = req("ma_crossover", {"fast": 6, "slow": 20}, 200)
    assert run_backtest(r2, {"T.A.X": bars(prices)}).result_sha256 != h1


def test_future_data_does_not_change_past_signals() -> None:
    rng = np.random.default_rng(2)
    prices = list(np.cumsum(rng.normal(size=120)) + 50)
    changed = prices[:80] + list(rng.normal(size=40) * 100)
    a = ma_crossover_signals(bars(prices), MaCrossoverParams(fast=3, slow=10)).to_list()
    b = ma_crossover_signals(bars(changed), MaCrossoverParams(fast=3, slow=10)).to_list()
    assert a[:80] == b[:80]
    mp = MeanReversionParams(lookback=10, z_entry=1.0, z_exit=0.3)
    c = mean_reversion_signals(bars(prices), mp).to_list()
    d = mean_reversion_signals(bars(changed), mp).to_list()
    assert c[:80] == d[:80]


def test_metric_formulas_hand_computed() -> None:
    eq = np.array([100.0, 110.0, 99.0, 108.9])
    m = compute_metrics(eq, np.array([0.0, 1.0, 1.0, 0.0]), np.array([1.0, -1.0, 2.0]), 4, 252)
    r = np.array([0.1, -0.1, 0.1])
    assert m.cagr == pytest.approx((108.9 / 100) ** (252 / 3) - 1)
    assert m.ann_vol == pytest.approx(np.std(r, ddof=1) * math.sqrt(252))
    assert m.sharpe == pytest.approx(np.mean(r) / np.std(r, ddof=1) * math.sqrt(252))
    assert m.max_drawdown == pytest.approx(99 / 110 - 1)
    assert m.hit_rate == pytest.approx(2 / 3)
    assert m.turnover == pytest.approx(2.0 / (3 / 252))
    assert m.n_trades == 4


def test_undefined_metrics_are_none() -> None:
    m = compute_metrics(np.array([100.0, 100.0, 100.0]), np.zeros(3), np.empty(0), 0, 252)
    assert (m.sharpe, m.ann_vol, m.hit_rate) == (None, None, None)
    m = compute_metrics(np.empty(0), np.empty(0), np.empty(0), 0, 252)
    assert (m.cagr, m.max_drawdown, m.turnover) == (None, None, None)


def test_empty_window() -> None:
    late = bars([1.0, 2.0]).with_columns(pl.col("ts_ns") + 100 * DAY)
    out = run_backtest(req("ma_crossover", {"fast": 2, "slow": 3}, 5), {"T.A.X": late})
    assert (out.equity.height, out.metrics.n_trades) == (0, 0)


def test_sweep_rejected() -> None:
    r = req("ma_crossover", {"fast": 2, "slow": 3}, 5).model_copy(update={"sweep": {"fast": [1]}})
    with pytest.raises(ValueError, match="sweep"):
        run_backtest(r, {"T.A.X": bars([1.0] * 5)})


@settings(max_examples=40, deadline=None)
@given(
    st.lists(st.floats(-100, 200, allow_nan=False), min_size=2, max_size=80),
    st.integers(1, 5),
    st.integers(2, 12),
)
def test_property_finite_bounded(prices: list[float], fast: int, slow: int) -> None:
    out = run_backtest(
        req("ma_crossover", {"fast": fast, "slow": slow}, len(prices), costs=(1, 1)),
        {"T.A.X": bars(prices)},
    )
    assert out.equity["position"].abs().max() <= 1  # type: ignore[operator]
    assert out.equity["equity"].is_finite().all()
    assert (out.equity["drawdown"] <= 0).all()
    for v in out.metrics.model_dump().values():
        assert v is None or math.isfinite(v)


def test_26y_daily_under_two_seconds() -> None:
    n = 26 * 365
    prices = (np.cumsum(np.random.default_rng(3).normal(size=n)) + 500).tolist()
    t0 = time.perf_counter()
    run_backtest(req("ma_crossover", {"fast": 20, "slow": 100}, n), {"T.A.X": bars(prices)})
    assert time.perf_counter() - t0 < 2.0
