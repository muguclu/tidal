"""aggregate_ticks: reference equivalence under arbitrary chunking, semantics, SYN month."""

import itertools
from datetime import date
from typing import Any

import numpy as np
import polars as pl
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tidal.common.errors import ValidationFailed
from tidal.common.timeutil import INTERVAL_NS, FixedClock
from tidal.contracts.schemas import BAR_V1, TICK_V1, Interval
from tidal.ingest.sources import synthetic
from tidal.ingest.validate import validate_and_flag
from tidal.storage.aggregate import aggregate_ticks

INST = "SYN.BRENT.TICK"
T0 = 1709251200 * 10**9  # 2024-03-01T00:00:00Z
DAY = INTERVAL_NS[Interval.D1]
COLS = [
    "ts_ns",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "quality",
    "ingested_at_ns",
]


def make_ticks(
    ts: list[int],
    price: list[float] | None = None,
    *,
    side: list[int] | None = None,
    version: list[int] | None = None,
    quality: list[int] | None = None,
    inst: str = INST,
) -> pa.Table:
    n = len(ts)
    return pa.table(
        {
            "ts_ns": pa.array(ts, pa.int64()),
            "instrument_id": pa.DictionaryArray.from_arrays(
                pa.array([0] * n, pa.int16()), pa.array([inst])
            ),
            "price": pa.array(price or [float(i) + 1.0 for i in range(n)], pa.float64()),
            "size": pa.array([0.5 + i % 3 for i in range(n)], pa.float32()),
            "side": pa.array(side or [0] * n, pa.int8()),
            "quality": pa.array(quality or [0] * n, pa.int8()),
            "ingested_at_ns": pa.array([T0 + 10 * i for i in range(n)], pa.int64()),
            "version": pa.array(version or [1] * n, pa.int16()),
        },
        schema=TICK_V1,
    )


def reference(ticks: pa.Table, interval: Interval) -> list[tuple[object, ...]]:
    """Naive single-frame groupby over the whole tick set (python loop, test only)."""
    step = INTERVAL_NS[interval]
    rows = [r for r in ticks.to_pylist() if r["side"] == 0]
    latest: dict[int, dict[str, Any]] = {}
    for r in rows:
        k = r["ts_ns"]
        if k not in latest or r["version"] >= latest[k]["version"]:
            latest[k] = r
    groups: dict[int, list[dict[str, Any]]] = {}
    for k in sorted(latest):
        groups.setdefault(k // step * step, []).append(latest[k])
    out: list[tuple[object, ...]] = []
    for bar, g in groups.items():
        p = [r["price"] for r in g]
        q = 0
        for r in g:
            q |= int(r["quality"])
        vol = sum(float(r["size"]) for r in g)
        ing = max(int(r["ingested_at_ns"]) for r in g)
        out.append((bar, p[0], max(p), min(p), p[-1], vol, len(g), q, ing))
    return out


def run(chunks: list[pa.Table], interval: Interval) -> pa.Table:
    out = list(aggregate_ticks(iter(chunks), interval))
    return pa.concat_tables(out) if out else BAR_V1.empty_table()


def as_rows(bars: pa.Table) -> list[tuple[object, ...]]:
    return [tuple(r[c] for c in COLS) for r in bars.to_pylist()]


@st.composite
def tick_sets(draw: st.DrawFn) -> pa.Table:
    n = draw(st.integers(1, 80))
    gaps = draw(st.lists(st.integers(0, 40 * 60 * 10**9), min_size=n, max_size=n))
    ts = (T0 + DAY // 2 + np.cumsum(gaps)).tolist()
    price = draw(st.lists(st.floats(-100, 100), min_size=n, max_size=n))
    side = draw(st.lists(st.integers(0, 2), min_size=n, max_size=n))
    seen: dict[tuple[int, int], int] = {}  # key (ts, side) repeats are restatements: version+1
    version = []
    for t, sd in zip(ts, side, strict=True):
        seen[t, sd] = seen.get((t, sd), 0) + 1
        version.append(seen[t, sd])
    quality = draw(st.lists(st.integers(0, 31), min_size=n, max_size=n))
    return make_ticks(ts, price, side=side, version=version, quality=quality)


@pytest.mark.parametrize("interval", list(Interval))
@given(ticks=tick_sets(), cuts=st.lists(st.integers(0, 80), max_size=6))
@settings(max_examples=60, deadline=None)
def test_any_chunking_matches_reference(
    ticks: pa.Table, cuts: list[int], interval: Interval
) -> None:
    bounds = sorted({0, ticks.num_rows, *(c for c in cuts if c <= ticks.num_rows)})
    chunks = [ticks.slice(a, b - a) for a, b in itertools.pairwise(bounds)]
    got = run(chunks, interval)
    assert as_rows(got) == reference(ticks, interval)
    assert got.schema.equals(BAR_V1)


def test_semantics_trades_only_negative_and_empty_intervals() -> None:
    minute = INTERVAL_NS[Interval.M1]
    ts = [T0 + 5, T0 + 10, T0 + 20, T0 + 3 * minute + 1, T0 + 3 * minute + 2]
    t = make_ticks(
        ts, [10.0, -37.63, 12.0, 5.0, 6.0], side=[0, 0, 1, 0, 2], quality=[0, 1, 0, 0, 0]
    )
    first, second = run([t], Interval.M1).to_pylist()
    assert (first["ts_ns"], second["ts_ns"]) == (T0, T0 + 3 * minute)  # minutes 1, 2: no bars
    assert (first["open"], first["high"], first["low"], first["close"]) == (
        10.0,
        10.0,
        -37.63,
        -37.63,
    )
    assert first["trade_count"] == 2
    assert first["quality"] == 1
    assert first["version"] == 1
    assert first["volume"] == pytest.approx(0.5 + 1.5)
    assert second["trade_count"] == 1  # quotes are ignored
    assert first["interval"] == "1m"
    assert first["instrument_id"] == INST
    assert first["ingested_at_ns"] == T0 + 10


def test_restated_uses_latest_version_across_chunks() -> None:
    t = make_ticks([T0 + 1, T0 + 1, T0 + 2], [1.0, 9.0, 2.0], version=[1, 2, 1], quality=[0, 8, 0])
    for cut in (1, 2):
        bar = run([t.slice(0, cut), t.slice(cut)], Interval.S1).to_pylist()[0]
        assert (bar["open"], bar["high"], bar["trade_count"], bar["quality"]) == (9.0, 9.0, 2, 8)


def test_hour_bar_split_across_chunks_and_daily_midnight() -> None:
    h = INTERVAL_NS[Interval.H1]
    a = make_ticks([T0 + h - 10, T0 + h - 5], [1.0, 2.0])
    b = make_ticks([T0 + h + 1], [3.0])
    c = make_ticks([T0 + h + 2, T0 + DAY + 7], [4.0, 5.0])
    out = list(aggregate_ticks(iter([a, b, c]), Interval.H1))
    assert [x.column("ts_ns").to_pylist() for x in out] == [[T0], [T0 + h], [T0 + DAY]]
    days = run([a, b, c], Interval.D1)
    assert days.column("ts_ns").to_pylist() == [T0, T0 + DAY]
    assert days.column("trade_count").to_pylist() == [4, 1]


def test_empty_and_quote_only_input() -> None:
    assert list(aggregate_ticks([], Interval.M1)) == []
    assert list(aggregate_ticks([make_ticks([T0], side=[1])], Interval.M1)) == []


def test_rejects_disorder_and_multiple_instruments() -> None:
    with pytest.raises(ValidationFailed):
        list(aggregate_ticks([make_ticks([T0 + 10]), make_ticks([T0 + 5])], Interval.D1))
    with pytest.raises(ValidationFailed):
        list(
            aggregate_ticks(
                [make_ticks([T0], inst="A"), make_ticks([T0 + 5], inst="B")], Interval.D1
            )
        )
    mixed = pa.concat_tables([make_ticks([T0], inst="A"), make_ticks([T0 + 1], inst="B")])
    with pytest.raises(ValidationFailed):
        list(aggregate_ticks([mixed.combine_chunks()], Interval.D1))


@pytest.mark.parametrize("interval", list(Interval))
def test_bars_validate_and_ohlc_invariants(interval: Interval) -> None:
    rng = np.random.default_rng(3)
    ts = (T0 + np.cumsum(rng.integers(1, 5 * 10**9, 3000))).tolist()
    price = (rng.standard_normal(3000).cumsum() - 20).tolist()  # goes negative
    bars = run([make_ticks(ts, price)], interval)
    d = pl.from_arrow(bars)
    assert isinstance(d, pl.DataFrame)
    ok = (
        (pl.col("low") <= pl.col("open"))
        & (pl.col("open") <= pl.col("high"))
        & (pl.col("low") <= pl.col("close"))
        & (pl.col("close") <= pl.col("high"))
    )
    assert d.select(ok.all()).item()
    assert (d["low"] < 0).any()
    assert validate_and_flag(bars).num_rows == bars.num_rows


def test_syn_month_s_scale_through_1m_and_1d() -> None:
    unit = synthetic.plan("S", date(2024, 3, 1), date(2024, 3, 31))[0]
    chunks = list(
        synthetic.generate_unit(unit, seed=42, scale="S", clock=FixedClock(T0), chunk_rows=2000)
    )
    assert len(chunks) > 1
    trades = sum(c.column("side").to_numpy().tolist().count(0) for c in chunks)
    for interval in (Interval.M1, Interval.D1):
        bars = run(chunks, interval)
        assert 0 < bars.num_rows <= trades
        assert int(bars.column("trade_count").to_numpy().sum()) == trades
        assert (np.diff(bars.column("ts_ns").to_numpy()) > 0).all()
        validate_and_flag(bars, synthetic=True)
    days = run(chunks, Interval.D1)
    assert days.num_rows <= 23  # business days of March 2024
    assert (days.column("ts_ns").to_numpy() % DAY == 0).all()
    one = pa.concat_tables(chunks).combine_chunks()
    assert run([one], Interval.M1).equals(run(chunks, Interval.M1))
