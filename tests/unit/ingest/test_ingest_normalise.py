import math

import numpy as np
import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tidal.common.errors import NotFound
from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import BAR_V1, QualityFlag
from tidal.ingest.normalise import normalise_page
from tidal.ingest.sources.eia import PAGE_SCHEMA

DAY = 86_400 * 10**9
T0 = 1_587_340_800 * 10**9  # 2020-04-20 00:00 UTC (a Monday)
CLOCK = FixedClock(1_700_000_000 * 10**9)


def page(values: list[float], series: str = "RWTC", step: int = DAY) -> pa.Table:
    n = len(values)
    return pa.table(
        {
            "ts_ns": T0 + np.arange(n, dtype=np.int64) * step,
            "value": np.array(values, dtype=np.float64),
            "series": [series] * n,
        },
        schema=PAGE_SCHEMA,
    )


def test_maps_value_to_flat_ohlc_with_nan_volume_and_clock() -> None:
    out = normalise_page(page([28.34, -36.98]), "EIA", CLOCK)
    t = out.bars
    assert t.schema.equals(BAR_V1, check_metadata=False)
    assert t.column("instrument_id").to_pylist() == ["EIA.WTI.SPOT"] * 2
    assert t.column("interval").to_pylist() == ["1d", "1d"]
    for col in ("open", "high", "low", "close"):
        assert t.column(col).to_pylist() == [28.34, -36.98]
    assert np.isnan(t.column("volume").to_numpy()).all()
    assert t.column("version").to_pylist() == [1, 1]
    assert set(t.column("ingested_at_ns").to_pylist()) == {CLOCK.now_ns()}
    assert t.column("trade_count").null_count == 2
    # NEGATIVE only on the negative row; negative price is not clamped
    assert t.column("quality").to_pylist() == [0, int(QualityFlag.NEGATIVE)]


def test_nan_rows_are_dropped_counted_and_never_filled() -> None:
    out = normalise_page(page([1.0, math.nan, 3.0]), "EIA", CLOCK)
    assert (out.rows_in, out.nan_dropped, out.bars.num_rows) == (3, 1, 2)
    assert out.bars.column("close").to_pylist() == [1.0, 3.0]
    assert out.bars.column("ts_ns").to_pylist() == [T0, T0 + 2 * DAY]


def test_all_nan_page_gives_empty_table() -> None:
    out = normalise_page(page([math.nan]), "EIA", CLOCK)
    assert out.bars.num_rows == 0
    assert out.bars.schema.equals(BAR_V1, check_metadata=False)


def test_fred_series_maps_to_fx_instrument() -> None:
    out = normalise_page(page([1.18], series="DEXUSEU"), "FRED", CLOCK)
    assert out.bars.column("instrument_id").to_pylist() == ["FRED.EURUSD.FX"]


def test_unknown_series_is_rejected() -> None:
    with pytest.raises(NotFound):
        normalise_page(page([1.0], series="NOPE"), "EIA", CLOCK)


@given(values=st.lists(st.one_of(st.floats(-1e4, 1e4), st.just(math.nan)), min_size=0, max_size=60))
def test_property_output_is_finite_flat_and_counts_add_up(values: list[float]) -> None:
    out = normalise_page(page(values), "EIA", CLOCK)
    good = [v for v in values if not math.isnan(v)]
    assert out.bars.num_rows == len(good) == out.rows_in - out.nan_dropped
    for col in ("open", "high", "low", "close"):
        assert out.bars.column(col).to_pylist() == good
    assert not np.isnan(out.bars.column("close").to_numpy()).any()
