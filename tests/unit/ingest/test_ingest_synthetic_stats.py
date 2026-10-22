"""Synthetic generator statistics: regime-vol targets (+-10 %), injection rates, U-shape."""

import numpy as np
import pyarrow as pa
import pytest

from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import QualityFlag
from tidal.ingest.sources import synthetic as syn

SEEDS = (11, 12, 13, 14)
OUT = int(QualityFlag.OUTLIER)
GAP = int(QualityFlag.GAP_BEFORE)
NS_DAY = 86_400_000_000_000


@pytest.mark.parametrize("instrument", syn.synthetic_instruments())
def test_regime_daily_vol_hits_param_targets_within_10pct(instrument: str) -> None:
    """Pooled over 4 seeds (about 63k business days), realised daily vol per regime is within
    +-10 % of the YAML target. This verifies the generator against its parameters; the parameters
    themselves are not yet fitted to real data (see the YAML header)."""
    p = syn.load_params(instrument)
    returns: list[list[np.ndarray]] = [[], [], []]
    for seed in SEEDS:
        path = syn.daily_path(instrument, seed)
        bus = path.business
        for k in range(3):
            returns[k].append(path.ret[bus][path.regime[bus] == k])
    for k in range(3):
        realised = float(np.concatenate(returns[k]).std())
        assert realised == pytest.approx(p.daily_vol[k], rel=0.10), (instrument, k)


def test_regime_occupancy_matches_stationary_distribution() -> None:
    p = syn.load_params("SYN.BRENT.TICK")
    path = syn.daily_path("SYN.BRENT.TICK", 11)
    freq = np.bincount(path.regime[path.business], minlength=3) / path.business.sum()
    assert freq == pytest.approx(p.stationary, abs=0.03)


def test_outlier_and_gap_injection_rates() -> None:
    n = 5_000_000
    rng = np.random.Generator(np.random.PCG64(5))
    assert syn.injection_mask_outliers(rng, n, 1e-4).mean() == pytest.approx(1e-4, rel=0.2)
    gaps = syn.injection_mask_gaps(rng, n, 1e-3, 20.0)
    assert gaps.mean() == pytest.approx(1e-3, rel=0.25)
    runs = np.flatnonzero(np.diff(gaps.astype(np.int8)) == 1).size
    assert 0.5 * n * 1e-3 / 20 < runs < 1.5 * n * 1e-3 / 20


def test_pipeline_flags_reflect_injected_faults() -> None:
    table = pa.concat_tables(
        syn.generate("SYN.WTI.TICK", 2022, 4, seed=3, scale="L", clock=FixedClock(0))
    )
    quality = table["quality"].to_numpy()
    n = table.num_rows
    assert int(((quality & GAP) != 0).sum()) == pytest.approx(n * 1e-3 / 20, rel=0.5)
    flagged = int(((quality & OUT) != 0).sum())
    # injected outliers + their reversion rows + a few real jumps
    assert 0.5 * n * 1e-4 < flagged < 5 * n * 1e-4


def test_u_shaped_intraday_activity() -> None:
    table = pa.concat_tables(
        syn.generate("SYN.BRENT.TICK", 2021, 5, seed=2, scale="L", clock=FixedClock(0))
    )
    hours = (table["ts_ns"].to_numpy() % NS_DAY) / 3_600_000_000_000
    p = syn.load_params("SYN.BRENT.TICK")
    assert hours.min() >= p.session_start_h
    assert hours.max() < p.session_end_h
    rel = (hours - p.session_start_h) / (p.session_end_h - p.session_start_h)
    edge = ((rel < 0.1) | (rel > 0.9)).mean() / 0.2
    middle = ((rel > 0.45) & (rel < 0.55)).mean() / 0.1
    assert edge > 2 * middle


def test_tick_daily_close_matches_daily_layer() -> None:
    """Last trade of each day is within bridge noise + half spread of the daily-layer close."""
    table = pa.concat_tables(
        syn.generate("SYN.HH.TICK", 2021, 5, seed=2, scale="L", clock=FixedClock(0))
    )
    ts, price, side = (table[c].to_numpy() for c in ("ts_ns", "price", "side"))
    day = ts // NS_DAY
    path = syn.daily_path("SYN.HH.TICK", 2)
    days = np.unique(day[side == 0])
    ok = 0
    for d in days:
        last = price[(day == d) & (side == 0)][-1]
        close = np.exp(path.x_close[d - syn._ANCHOR_DAY])
        ok += abs(last / close - 1) < 0.01
    assert ok >= 0.9 * days.size  # a 0.01 % outlier may hit a few days
