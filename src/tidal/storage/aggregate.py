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
* Dedup is unconditional, so the result never depends on chunking. Two trades with the same
  ``(ts_ns, side, version)`` violate the §2 primary key and raise ``ValidationFailed``.
* A bar's ``quality`` may carry ``RESTATED`` (OR of tick flags) while the bar ``version`` is 1;
  this is intended (§3.2, §5): derived bars are never restated themselves.
* An interval can span chunks. The raw trades of the trailing (possibly incomplete) interval are
  carried as a list of frames (concatenated only when the interval changes, so cost is linear)
  and aggregated once complete. Chosen over carrying partial aggregates because dedup needs all
  versions of a key. Limitation: memory is one input chunk plus the raw trades of one interval
  (for ``1d`` over many small chunks, one day of trades).
* ``BarAggregator`` is the push-style form (one pass serves several intervals);
  ``aggregate_ticks`` wraps it for a single interval.
"""

from collections.abc import Iterable, Iterator, Sequence

import polars as pl
import pyarrow as pa

from tidal.common.errors import ValidationFailed
from tidal.common.timeutil import INTERVAL_NS
from tidal.contracts.schemas import BAR_V1, Interval, TickSide

_COLS = ("ts_ns", "price", "size", "quality", "ingested_at_ns", "version")


def _trades(table: pa.Table) -> tuple[pl.DataFrame, str | None]:
    """Trade rows of ``table`` as a polars frame plus the (single) instrument id."""
    df = pl.from_arrow(table.select(["ts_ns", "instrument_id", *_COLS[1:], "side"]))
    assert isinstance(df, pl.DataFrame)  # noqa: S101 - from_arrow(Table) always gives a DataFrame
    df = df.filter(pl.col("side") == int(TickSide.TRADE)).drop("side")
    if df.is_empty():
        return df.drop("instrument_id"), None
    ids = df["instrument_id"].cast(pl.String).unique()
    if ids.len() != 1:
        msg = f"aggregate_ticks expects one instrument per call, got {ids.len()}"
        raise ValidationFailed(msg)
    return df.drop("instrument_id"), str(ids[0])


def _dedup(df: pl.DataFrame) -> pl.DataFrame:
    """Latest version per ``ts_ns`` (side is 0 here); duplicate ``(ts_ns, version)`` is an error."""
    if df["ts_ns"].diff().drop_nulls().gt(0).all():
        return df  # strictly increasing: no repeated key
    df = df.sort("ts_ns", "version", maintain_order=True)
    dup = (pl.col("ts_ns") == pl.col("ts_ns").shift()) & (
        pl.col("version") == pl.col("version").shift()
    )
    if df.select(dup.any()).item():
        msg = "duplicate tick key (ts_ns, side, version) violates the data-contracts §2 PK"
        raise ValidationFailed(msg)
    return df.unique(subset="ts_ns", keep="last", maintain_order=True)


def _bars(df: pl.DataFrame, instrument_id: str, interval: Interval) -> pa.Table:
    """Aggregate ts-sorted trades into one ``BAR_V1`` table."""
    step = INTERVAL_NS[interval]
    out = (
        _dedup(df)
        .with_columns((pl.col("ts_ns") - pl.col("ts_ns") % step).alias("bar"))
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
    code = pa.DictionaryArray.from_arrays(pa.array([0] * n, pa.int8()), pa.array([interval.value]))
    extra = {"instrument_id": inst, "interval": code, "version": pa.array([1] * n, pa.int16())}
    arrays = [extra[f.name] if f.name in extra else out.column(f.name) for f in BAR_V1]
    return pa.Table.from_arrays(arrays, names=BAR_V1.names).cast(BAR_V1)


class BarAggregator:
    """Push-style multi-interval aggregator for sorted ``TICK_V1`` chunks of one instrument."""

    def __init__(self, intervals: Sequence[Interval]) -> None:
        """Aggregate into each of ``intervals`` (duplicates collapse) in a single pass."""
        self._intervals = tuple(dict.fromkeys(Interval(i) for i in intervals))
        self._pending: dict[Interval, list[pl.DataFrame]] = {i: [] for i in self._intervals}
        self._start: dict[Interval, int] = {}  # interval start of the pending rows
        self._inst: str | None = None
        self._last_ts: int | None = None

    def feed(self, table: pa.Table) -> dict[Interval, list[pa.Table]]:
        """Consume one tick chunk; return the bars completed so far (possibly empty lists).

        Raises:
            ValidationFailed: more than one instrument, ``ts_ns`` not non-decreasing within or
                across chunks, or a duplicate ``(ts_ns, side, version)`` key.
        """
        out: dict[Interval, list[pa.Table]] = {i: [] for i in self._intervals}
        df, inst = _trades(table)
        if inst is None:
            return out
        if self._inst is not None and inst != self._inst:
            msg = f"aggregate_ticks expects one instrument per call, got {self._inst} and {inst}"
            raise ValidationFailed(msg)
        self._inst = inst
        ts = df["ts_ns"]
        lo, hi = int(ts.min()), int(ts.max())  # type: ignore[arg-type]  # non-empty int column
        if not ts.is_sorted() or (self._last_ts is not None and lo < self._last_ts):
            msg = "tick chunks are not in ts_ns order"
            raise ValidationFailed(msg)
        self._last_ts = hi
        for iv in self._intervals:
            step = INTERVAL_NS[iv]
            last = hi // step * step
            pend = self._pending[iv]
            if pend and self._start[iv] == last and lo >= last:
                pend.append(df)  # whole chunk inside the pending interval: no concat
                continue
            full = pl.concat([*pend, df]) if pend else df
            done = full.filter(pl.col("ts_ns") < last)
            if not done.is_empty():
                out[iv].append(_bars(done, inst, iv))
            self._pending[iv] = [full.filter(pl.col("ts_ns") >= last)]
            self._start[iv] = last
        return out

    def flush(self) -> dict[Interval, list[pa.Table]]:
        """Emit the trailing (possibly partial) bars of each interval and reset the state."""
        out: dict[Interval, list[pa.Table]] = {i: [] for i in self._intervals}
        if self._inst is not None:
            for iv, pend in self._pending.items():
                if pend:
                    out[iv].append(_bars(pl.concat(pend), self._inst, iv))
        self._pending = {i: [] for i in self._intervals}
        self._start = {}
        self._inst = None
        self._last_ts = None
        return out


def aggregate_ticks(batches: Iterable[pa.Table], interval: Interval) -> Iterator[pa.Table]:
    """Yield ``BAR_V1`` tables for ``interval`` from sorted ``TICK_V1`` chunks of one instrument.

    Every yielded table holds only complete bars, strictly increasing in ``ts_ns`` and continuing
    where the previous one stopped; the last (possibly partial) interval is emitted at end of
    input. See the module docstring for semantics. Thin wrapper over ``BarAggregator``.

    Raises:
        ValidationFailed: see ``BarAggregator.feed``.
    """
    iv = Interval(interval)
    agg = BarAggregator([iv])
    for batch in batches:
        yield from agg.feed(batch)[iv]
    yield from agg.flush()[iv]
