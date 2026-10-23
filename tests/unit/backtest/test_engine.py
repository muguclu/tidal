"""T-301 unit tests: strategies, execution timing, costs, metrics, contract validity."""

import math
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
from tidal.common.errors import ValidationFailed
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
    unit = 1_000_000.0 / PRICES[0]  # sized at the first fill
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


def test_negative_prices_additive_equity_hand_computed() -> None:
    prices = [50.0, 52.0, 40.0, -10.0, -20.0, -30.0]
    out = run_backtest(req("ma_crossover", {"fast": 1, "slow": 2}, 6), {"T.A.X": bars(prices)})
    q = 1_000_000.0 / 50.0
    assert out.equity["position"].to_list() == [0, 0, 1, -1, -1, -1]
    # long q from 40 -> -10 loses 50q (equity 0); flip to short, -10 -> -20 gains 10q, then 10q.
    assert out.equity["equity"].to_list() == pytest.approx(
        [1e6, 1e6, 1e6, 1e6 - 50 * q, 1e6 - 40 * q, 1e6 - 30 * q]
    )
    assert out.trades["price"].to_list() == [40.0, -10.0]
    assert out.trades["qty"].to_list() == pytest.approx([q, -2 * q])


def test_mean_reversion_hysteresis_exact() -> None:
    p = MeanReversionParams(lookback=3, z_entry=1.0, z_exit=0.2)
    sig = mean_reversion_signals(bars([10, 10, 20, 17, 16, 10, 10, 10.5]), p).to_list()
    # idx2 (10,10,20): z=+1.155 -> short; idx3 (10,20,17): z=0.29 in (exit, entry) -> hold;
    # idx4 (20,17,16): z=-0.8... mean 17.67 sd 2.08 z=-0.8 -> hold; idx5 (17,16,10):
    # mean 14.33 sd 3.79 z=-1.14 -> long; idx6 (16,10,10): z=-0.577 hold;
    # idx7 (10,10,10.5): mean 10.17 sd .289 z=+1.15 -> short again.
    assert sig == [0, 0, -1, -1, -1, 1, 1, -1]


def test_mean_reversion_exit_and_long() -> None:
    p = MeanReversionParams(lookback=3, z_entry=1.0, z_exit=0.2)
    # idx2 (10,10,5): z=-1.155 -> long; idx3 (10,5,7.5): z=0 -> flat.
    sig = mean_reversion_signals(bars([10, 10, 5, 7.5]), p).to_list()
    assert sig == [0, 0, 1, 0]


def test_calendar_spread_trades_both_legs() -> None:
    a = bars([10, 11, 12, 13, 14, 15, 16.0])
    b = bars([9.0] * 7)
    ids = ("T.A.X", "T.B.X")
    params = {"lookback": 3, "z_entry": 0.5, "z_exit": 0.1}
    free = run_backtest(req("calendar_spread", params, 7, ids=ids), {ids[0]: a, ids[1]: b})
    q = 1e6 / (10 + 9)
    t = free.trades
    # spread 1..7 rises linearly: z=+1 -> short spread from the fill at idx3 (spread 4)
    assert t["instrument_id"].to_list() == list(ids)
    assert t["qty"].to_list() == pytest.approx([-q, q])
    assert t["price"].to_list() == [13.0, 9.0]
    assert free.equity["equity"][-1] == pytest.approx(1e6 - q * (7 - 4))
    costly = run_backtest(
        req("calendar_spread", params, 7, ids=ids, costs=(10.0, 4.0)), {ids[0]: a, ids[1]: b}
    )
    assert costly.trades["fee"].to_list() == pytest.approx([10 / 1e4 * q * 13, 10 / 1e4 * q * 9])
    assert costly.trades["slippage"].to_list() == pytest.approx([4 / 1e4 * q * 13, 4 / 1e4 * q * 9])
    total = float((costly.trades["fee"] + costly.trades["slippage"]).sum())
    assert free.equity["equity"][-1] - costly.equity["equity"][-1] == pytest.approx(total)


def test_intraday_fills_at_next_open() -> None:
    close = [1.0, 2.0, 3.0, 2.0, 1.0]
    open_ = [1.1, 2.1, 3.1, 2.1, 1.1]
    hour = 3_600 * 10**9
    df = pl.DataFrame({"ts_ns": [T0 + i * hour for i in range(5)], "open": open_, "close": close})
    out = run_backtest(req("ma_crossover", {"fast": 1, "slow": 2}, 5, interval="1h"), {"T.A.X": df})
    q = 1e6 / 1.1
    assert out.equity["position"].to_list() == [0, 0, 1, 1, -1]
    assert out.trades["price"].to_list() == pytest.approx([3.1, 1.1])
    assert out.equity["equity"][4] == pytest.approx(1e6 + q * (1.1 - 3.1))


def test_missing_instrument_and_sweep_raise_validation_failed() -> None:
    with pytest.raises(ValidationFailed):
        run_backtest(req("ma_crossover", {"fast": 2, "slow": 3}, 5), {})


def _walk(seed: int, n: int = 160) -> list[float]:
    return (np.cumsum(np.random.default_rng(seed).normal(size=n)) + 100).tolist()


@pytest.mark.parametrize(
    ("strategy", "params", "ids"),
    [
        ("ma_crossover", {"fast": 3, "slow": 10}, ("T.A.X",)),
        ("mean_reversion", {"lookback": 10, "z_entry": 1.0, "z_exit": 0.3}, ("T.A.X",)),
        ("calendar_spread", {"lookback": 10, "z_entry": 1.0, "z_exit": 0.3}, ("T.A.X", "T.B.X")),
    ],
)
@pytest.mark.parametrize("mutation", ["scale10", "random"])
def test_engine_future_changes_do_not_alter_past(
    strategy: str, params: dict[str, Any], ids: tuple[str, ...], mutation: str
) -> None:
    k = 80
    base = {i: _walk(n + 1) for n, i in enumerate(ids)}
    changed = {}
    for i, px in base.items():
        if mutation == "scale10":
            tail = [x * 10 for x in px[k:]]
        else:
            tail = (np.random.default_rng(99).normal(size=len(px) - k) * 50 - 20).tolist()
        changed[i] = px[:k] + tail
    r = req(strategy, params, 160, ids=ids, costs=(1.0, 2.0))
    o1 = run_backtest(r, {i: bars(p) for i, p in base.items()})
    o2 = run_backtest(r, {i: bars(p) for i, p in changed.items()})
    ts_k = T0 + k * DAY
    for col in ("ts_ns", "equity", "position", "drawdown"):
        assert o1.equity[col][:k].to_list() == o2.equity[col][:k].to_list()
    t1 = o1.trades.filter(pl.col("ts_ns") < ts_k)
    t2 = o2.trades.filter(pl.col("ts_ns") < ts_k)
    assert t1.equals(t2)


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
    with pytest.raises(ValidationFailed):
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
