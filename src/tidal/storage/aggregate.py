"""Streaming tick -> bar aggregation (T-107, data-contracts §3.2, ADR-0009).

``aggregate_ticks`` turns sorted ``TICK_V1`` chunks of ONE instrument into ``BAR_V1`` tables:

* Only trades (``side == 0``) contribute; quotes are ignored. Empty intervals produce no bar.
* ``open``/``close`` = first/last trade, ``high``/``low`` = max/min (negative prices kept),
  ``volume`` = sum of ``size`` (accumulated in float64), ``trade_count`` = number of trades,
  ``quality`` = bitwise OR of the tick flags (reserved bits stay 0), ``version`` = 1,
  ``ingested_at_ns`` = max of the ticks' ``ingested_at_ns``.
* Bar ``ts_ns`` = the UTC-aligned interval start (``1d`` = UTC midnight of the trade date,
  ADR-0009).
* Restatements: bars are built from the **latest view** (§7): per tick key ``(ts_ns, side=0)``
  only the row with the highest ``version`` counts. As-of bars (``ingested_at_ns <= T``) are out
  of scope; they are computed on read.
* An interval can span chunks. The raw trades of the trailing (possibly incomplete) interval are
  carried to the next chunk, so all versions of a key meet before dedup. Memory is therefore
  bounded by one input chunk plus one interval's trades (a ``1d`` interval of a day-block chunk
  is at most that chunk).
"""

from collections.abc import Iterable, Iterator

import polars as pl
import pyarrow as pa

from tidal.common.errors import ValidationFailed
from tidal.common.timeutil import INTERVAL_NS
from tidal.contracts.schemas import BAR_V1, Interval, TickSide

_COLS = ("ts_ns", "price", "size", "quality", "ingested_at_ns", "version")


def _trades(table: pa.Table) -> tuple[pl.DataFrame, str | None]:
    """Trade rows of ``table`` as a polars frame plus the (single) instrument id."""
    df = pl.from_arrow(table.select(["ts_ns", "instrument_id", *_COLS[1:], "side"]))
    assert isinstance(df, pl.DataFrame)  # noqa: S101 - from_arrow(Table) is a DataFrame
    df = df.filter(pl.col("side") == int(TickSide.TRADE)).drop("side")
    if df.is_empty():
        return df.drop("instrument_id"), None
    ids = df["instrument_id"].cast(pl.String).unique()
    if ids.len() != 1:
        msg = f"aggregate_ticks expects one instrument per call, got {ids.len()}"
        raise ValidationFailed(msg)
    return df.drop("instrument_id"), str(ids[0])


def _bars(df: pl.DataFrame, instrument_id: str, interval: str, step: int) -> pa.Table:
    """Aggregate de-duplicated, ts-sorted trades into one ``BAR_V1`` table."""
    if df["version"].max() > 1:  # type: ignore[operator]  # non-empty frame, int column
        df = df.sort("ts_ns", "version").unique(subset="ts_ns", keep="last", maintain_order=True)
    out = (
        df.with_columns((pl.col("ts_ns") - pl.col("ts_ns") % step).alias("bar"))
        .group_by("bar", maintain_order=True)
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.col("size").cast(pl.Float64).sum().alias("volume"),
            pl.len().cast(pl.Int32).alias("trade_count"),
            pl.col("quality").bitwise_or().cast(pl.Int8),
            pl.col("ingested_at_ns").max(),
        )
        .rename({"bar": "ts_ns"})
        .to_arrow()
    )
    n = out.num_rows
    inst = pa.DictionaryArray.from_arrays(pa.array([0] * n, pa.int16()), pa.array([instrument_id]))
    code = pa.DictionaryArray.from_arrays(pa.array([0] * n, pa.int8()), pa.array([interval]))
    extra = {"instrument_id": inst, "interval": code, "version": pa.array([1] * n, pa.int16())}
    arrays = [extra[f.name] if f.name in extra else out.column(f.name) for f in BAR_V1]
    return pa.Table.from_arrays(arrays, names=BAR_V1.names).cast(BAR_V1)


def aggregate_ticks(batches: Iterable[pa.Table], interval: Interval) -> Iterator[pa.Table]:
    """Yield ``BAR_V1`` tables for ``interval`` from sorted ``TICK_V1`` chunks of one instrument.

    Every yielded table holds only complete bars, strictly increasing in ``ts_ns`` and continuing
    where the previous one stopped; the last (possibly partial) interval is emitted at end of
    input. Chunks must be consecutive and sorted by ``ts_ns``.

    Raises:
        ValidationFailed: more than one instrument, or chunks out of ``ts_ns`` order.
    """
    key = Interval(interval).value
    step = INTERVAL_NS[Interval(interval)]
    carry: pl.DataFrame | None = None
    inst: str | None = None
    for batch in batches:
        df, bid = _trades(batch)
        if df.is_empty():
            continue
        if inst is not None and bid != inst:
            msg = f"aggregate_ticks expects one instrument per call, got {inst} and {bid}"
            raise ValidationFailed(msg)
        inst = bid
        if carry is not None:
            if df["ts_ns"].min() < carry["ts_ns"].max():  # type: ignore[operator]
                msg = "tick chunks are not in ts_ns order"
                raise ValidationFailed(msg)
            df = pl.concat([carry, df])
        last = int(df.select(pl.col("ts_ns").max()).item()) // step * step
        done, carry = df.filter(pl.col("ts_ns") < last), df.filter(pl.col("ts_ns") >= last)
        if not done.is_empty():
            assert inst is not None  # noqa: S101
            yield _bars(done, inst, key, step)
    if carry is not None and inst is not None and not carry.is_empty():
        yield _bars(carry, inst, key, step)
