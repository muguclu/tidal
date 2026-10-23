"""T-302 look-ahead suite: mutating bars >= k never changes anything observable before k."""

from typing import Any

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tidal.backtest import (
    calendar_spread_signals,
    ma_crossover_signals,
    mean_reversion_signals,
    run_backtest,
)
from tidal.contracts.models import MaCrossoverParams, MeanReversionParams
from unit.backtest._bt_support import make_bars, make_request

T0 = 1_577_836_800 * 10**9
STEPS = {"1d": 86_400 * 10**9, "1h": 3_600 * 10**9}
MR = {"lookback": 4, "z_entry": 1.0, "z_exit": 0.25}
STRATS: dict[str, dict[str, Any]] = {
    "ma_crossover": {"fast": 2, "slow": 5},
    "mean_reversion": MR,
    "calendar_spread": MR,
}

price = st.floats(min_value=-100.0, max_value=300.0, allow_nan=False, allow_infinity=False)
LOOKAHEAD = settings(max_examples=60)  # bounded; the ci profile still derandomises (conftest)


@st.composite
def scenario(draw: st.DrawFn) -> dict[str, Any]:
    n = draw(st.integers(min_value=8, max_value=40))

    def series() -> list[float]:
        return draw(st.lists(price, min_size=n, max_size=n))

    return {
        "n": n,
        "k": draw(st.integers(min_value=1, max_value=n - 1)),
        "a_open": series(),
        "a_close": series(),
        "b_open": series(),
        "b_close": series(),
        "scale": draw(st.floats(min_value=-5.0, max_value=5.0, allow_nan=False)),
        "noise": series(),
        "interval": draw(st.sampled_from(["1d", "1h"])),
        "strategy": draw(st.sampled_from(sorted(STRATS))),
    }


def _mutate(xs: list[float], k: int, scale: float, noise: list[float]) -> list[float]:
    return [x if i < k else x * scale + noise[i] for i, x in enumerate(xs)]


@LOOKAHEAD
@given(sc=scenario())
def test_future_mutation_leaves_past_unchanged(sc: dict[str, Any]) -> None:
    n, k, interval, strategy = sc["n"], sc["k"], sc["interval"], sc["strategy"]
    step = STEPS[interval]
    ids = ["T.A.X", "T.B.X"] if strategy == "calendar_spread" else ["T.A.X"]
    req = make_request(strategy, STRATS[strategy], ids, interval, T0, step, n)
    cols = {"T.A.X": (sc["a_open"], sc["a_close"]), "T.B.X": (sc["b_open"], sc["b_close"])}

    def build(mutate: bool) -> dict[str, pl.DataFrame]:
        out = {}
        for iid in ids:
            o, c = cols[iid]
            if mutate:
                o = _mutate(o, k, sc["scale"], sc["noise"])
                c = _mutate(c, k, sc["scale"], sc["noise"][::-1])
            out[iid] = make_bars(T0, step, o, c)
        return out

    base = run_backtest(req, build(False))
    mut = run_backtest(req, build(True))

    assert mut.equity[:k].equals(base.equity[:k])  # equity, position, drawdown before k
    ts_k = T0 + k * step
    assert mut.trades.filter(pl.col("ts_ns") < ts_k).equals(
        base.trades.filter(pl.col("ts_ns") < ts_k)
    )


@LOOKAHEAD
@given(sc=scenario())
def test_signals_prefix_independent_of_future(sc: dict[str, Any]) -> None:
    """Strategy level: signal[:k] depends only on bars[:k] (SPEC-05 section 5)."""
    k = sc["k"]
    step = STEPS["1d"]
    full = make_bars(T0, step, sc["a_open"], sc["a_close"])
    mut = make_bars(T0, step, sc["a_open"], _mutate(sc["a_close"], k, sc["scale"], sc["noise"]))
    ma = MaCrossoverParams(fast=2, slow=5)
    mr = MeanReversionParams(lookback=4, z_entry=1.0, z_exit=0.25)
    assert (
        ma_crossover_signals(full, ma)[:k].to_list() == ma_crossover_signals(mut, ma)[:k].to_list()
    )
    for fn in (mean_reversion_signals, calendar_spread_signals):
        assert fn(full, mr)[:k].to_list() == fn(mut, mr)[:k].to_list()


@pytest.mark.parametrize("strategy", sorted(STRATS))
def test_future_truncation_equals_prefix(strategy: str) -> None:
    """Running on bars[:k] reproduces the first k equity rows of the full run (fixed series)."""
    n, k = 30, 17
    a = [100.0 + 7 * ((i * 5) % 9) - 3 * (i % 4) for i in range(n)]
    b = [95.0 + 2 * (i % 5) for i in range(n)]
    ids = ["T.A.X", "T.B.X"] if strategy == "calendar_spread" else ["T.A.X"]
    step = STEPS["1d"]
    req = make_request(strategy, STRATS[strategy], ids, "1d", T0, step, n)
    full = {"T.A.X": make_bars(T0, step, a, a), "T.B.X": make_bars(T0, step, b, b)}
    cut = {i: f.head(k) for i, f in full.items()}
    assert run_backtest(req, cut).equity.equals(run_backtest(req, full).equity[:k])
