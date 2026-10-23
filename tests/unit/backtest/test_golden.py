"""T-302 golden suite: the engine must reproduce the hand-derived numbers in the fixtures."""

import math
import statistics
from pathlib import Path

import pytest

from tidal.backtest import run_backtest
from unit.backtest._bt_support import fixture_paths, load_fixture, request_and_bars

PATHS = fixture_paths()
IDS = [p.stem for p in PATHS]


def test_fixtures_present() -> None:
    assert len(PATHS) >= 4
    strategies = {load_fixture(p)["strategy"] for p in PATHS}
    assert strategies == {"ma_crossover", "mean_reversion", "calendar_spread"}


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_golden_equity_position_trades(path: Path) -> None:
    fx = load_fixture(path)
    exp = fx["expected"]
    out = run_backtest(*request_and_bars(fx))
    t0, step = fx["t0_ns"], fx["step_ns"]

    assert out.equity["ts_ns"].to_list() == [t0 + i * step for i in range(len(exp["equity"]))]
    assert out.equity["position"].to_list() == pytest.approx(exp["position"], abs=1e-12)
    assert out.equity["equity"].to_list() == pytest.approx(exp["equity"], rel=1e-12, abs=1e-6)
    assert out.equity["drawdown"].to_list() == pytest.approx(exp["drawdown"], rel=1e-9, abs=1e-12)

    assert out.trades.height == len(exp["trades"])
    assert out.trades["ts_ns"].to_list() == [t0 + t["idx"] * step for t in exp["trades"]]
    assert out.trades["instrument_id"].to_list() == [t["instrument_id"] for t in exp["trades"]]
    for col in ("qty", "price", "fee", "slippage"):
        assert out.trades[col].to_list() == pytest.approx(
            [t[col] for t in exp["trades"]], rel=1e-12, abs=1e-9
        ), col


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_golden_metrics(path: Path) -> None:
    fx = load_fixture(path)
    exp = fx["expected"]
    m = run_backtest(*request_and_bars(fx)).metrics
    eq = [float(x) for x in exp["equity"]]
    ppy = exp["periods_per_year"]
    n_periods = len(eq) - 1
    r = [eq[i + 1] / eq[i] - 1 for i in range(n_periods)]  # SPEC-05 section 3 simple returns

    assert m.n_trades == exp["n_trades"]
    assert m.hit_rate == pytest.approx(exp["hit_rate"])
    assert m.max_drawdown == pytest.approx(exp["max_drawdown"], rel=1e-9)
    assert m.cagr == pytest.approx((eq[-1] / eq[0]) ** (ppy / n_periods) - 1, rel=1e-9, abs=1e-12)
    sd = statistics.stdev(r)
    assert m.ann_vol == pytest.approx(sd * math.sqrt(ppy), rel=1e-9)
    assert m.sharpe == pytest.approx(statistics.fmean(r) / sd * math.sqrt(ppy), rel=1e-9)
    years = n_periods / ppy
    assert m.turnover == pytest.approx(exp["total_abs_position_change"] / years, rel=1e-9)


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_golden_costs_reconcile(path: Path) -> None:
    """Recorded fees+slippage equal the hand cost bookkeeping of the fixture."""
    fx = load_fixture(path)
    out = run_backtest(*request_and_bars(fx))
    hand = sum(t["fee"] + t["slippage"] for t in fx["expected"]["trades"])
    assert float((out.trades["fee"] + out.trades["slippage"]).sum()) == pytest.approx(hand)


def test_negative_price_fixture_really_negative() -> None:
    fx = load_fixture(next(p for p in PATHS if "negative" in p.stem))
    assert min(fx["bars"]["T.A.X"]["close"]) < 0
    out = run_backtest(*request_and_bars(fx))
    assert (out.trades["price"] < 0).any()  # a fill happened at a negative price, unclamped


def test_calendar_spread_fixture_has_two_rows_per_fill() -> None:
    fx = load_fixture(next(p for p in PATHS if "calendar" in p.stem))
    out = run_backtest(*request_and_bars(fx))
    assert set(out.trades.group_by("ts_ns").len()["len"].to_list()) == {2}
    qty = out.trades["qty"].to_list()
    for i in range(0, len(qty), 2):
        assert qty[i] == pytest.approx(-qty[i + 1])  # +q leg_a / -q leg_b
