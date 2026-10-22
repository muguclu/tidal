"""EIA/FRED parsed pages -> validated ``BAR_V1`` daily bars (SPEC-01 §2, data-contracts §3/§3.1).

``open = high = low = close = value`` and ``volume = NaN`` (no volume for these sources). NaN
values (EIA null, FRED ``"."``) are dropped, counted and logged, never filled: OHLC is never NaN.
Negative prices are kept (``validate_and_flag`` adds ``NEGATIVE``). ``version`` is 1; the hot
store assigns ``version+1`` on restatement. ``ingested_at_ns`` comes from the injected clock.
"""

from dataclasses import dataclass

import numpy as np
import polars as pl
import pyarrow as pa

from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock
from tidal.contracts.schemas import BAR_V1, Interval
from tidal.ingest.sources.base import instrument_for
from tidal.ingest.validate import validate_and_flag

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Normalised:
    """Bars of one page plus bookkeeping: rows in the page and NaN rows dropped."""

    bars: pa.Table
    rows_in: int
    nan_dropped: int


def normalise_page(page: pa.Table, source: str, clock: Clock) -> Normalised:
    """Convert a parsed page ``(ts_ns, value, series)`` to flagged ``BAR_V1`` ``1d`` bars."""
    rows_in = page.num_rows
    df = pl.from_arrow(page)
    assert isinstance(df, pl.DataFrame)  # noqa: S101 - from_arrow(Table) is always a DataFrame
    ids = {s: instrument_for(source, s) for s in df["series"].unique().to_list()}
    kept = df.filter(~pl.col("value").is_nan() & pl.col("value").is_not_null())
    kept = kept.with_columns(
        pl.col("series").replace_strict(ids, return_dtype=pl.String).alias("instrument_id")
    ).sort("instrument_id", "ts_ns", maintain_order=True)
    dropped = rows_in - kept.height
    if dropped:
        _log.warning("nan_values_dropped", source=source, dropped=dropped, rows=rows_in)
    n = kept.height
    value = kept["value"].to_numpy()
    inst = kept["instrument_id"].to_arrow().dictionary_encode()
    arrays = {
        "ts_ns": kept["ts_ns"].to_arrow(),
        "instrument_id": pa.DictionaryArray.from_arrays(
            inst.indices.cast(pa.int16()), inst.dictionary
        ),
        "interval": pa.DictionaryArray.from_arrays(
            np.zeros(n, np.int8), pa.array([Interval.D1.value])
        ),
        **{c: pa.array(value) for c in ("open", "high", "low", "close")},
        "volume": pa.array(np.full(n, np.nan)),
        "trade_count": pa.nulls(n, pa.int32()),
        "quality": pa.array(np.zeros(n, np.int8)),
        "ingested_at_ns": pa.array(np.full(n, clock.now_ns(), np.int64)),
        "version": pa.array(np.ones(n, np.int16)),
    }
    table = pa.Table.from_arrays([arrays[f.name] for f in BAR_V1], schema=BAR_V1)
    return Normalised(validate_and_flag(table), rows_in, dropped)
