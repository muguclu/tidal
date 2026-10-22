"""Unified reader: SQL hot store plus Parquet cold store, split at the watermark (SPEC-03 §5).

Cold rows (``ts_ns < watermark``) come from DuckDB over hive-partitioned Parquet, hot rows from SQL;
both are concatenated with polars and versions are resolved per data-contracts §7 (latest, or the
max version with ``ingested_at_ns <= as_of``). Streaming reads walk the range in fixed time slices
so memory is bounded by one slice, never by the range.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import polars as pl
import pyarrow as pa
from sqlalchemy import Engine, Table, select

from tidal.common.config import Settings
from tidal.common.errors import NotFound
from tidal.common.timeutil import Clock, from_ts_ns
from tidal.contracts.instruments import AssetClass, Instrument, Source
from tidal.contracts.models import Bar, LatestBar, Tick, Tier
from tidal.contracts.schemas import BAR_V1, INTERVAL_CODES, TICK_V1, Interval, TickSide
from tidal.query.duck import DuckSession, glob_for
from tidal.query.protocols import Rows, RowStream
from tidal.storage import sql_repo as sr
from tidal.storage.layout import DAILY_DATASET

_NS_DAY: Final = 86_400 * 10**9
#: Stream slice width per dataset: keeps one slice to roughly <= 1e5-1e6 rows.
_SLICE_NS: Final[dict[str, int]] = {
    "ticks": _NS_DAY,
    "bars_1s": _NS_DAY,
    "bars_1m": 31 * _NS_DAY,
    "bars_1h": 366 * _NS_DAY,
    "bars_1d": 3660 * _NS_DAY,
}
#: Hive options: ``month=00`` (daily bars) must still type as an integer for pruning filters.
_HIVE: Final = "hive_partitioning=1, hive_types={'year': BIGINT, 'month': BIGINT}"
_BATCH_ROWS: Final = 65_536
_DTYPES: Final[dict[str, pl.DataType]] = {
    "ts_ns": pl.Int64(), "ingested_at_ns": pl.Int64(), "quality": pl.Int8(),
    "version": pl.Int16(), "side": pl.Int8(), "trade_count": pl.Int32(), "size": pl.Float32(),
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Kind:
    dataset_prefix: str
    table: Table
    columns: tuple[str, ...]  # value + bookkeeping columns, in file order
    key: tuple[str, ...]  # versionless key within one instrument (and interval)
    schema: pa.Schema


_BAR_COLS = [
    *("ts_ns", "open", "high", "low", "close", "volume", "trade_count"),
    *("quality", "ingested_at_ns", "version"),
]
_TICK_COLS = ["ts_ns", "price", "size", "side", "quality", "ingested_at_ns", "version"]
_BARS = _Kind("bars_", sr.bar_hot, tuple(_BAR_COLS), ("ts_ns",), BAR_V1)
_TICKS = _Kind("ticks", sr.tick_hot, tuple(_TICK_COLS), ("ts_ns", "side"), TICK_V1)


@dataclass(frozen=True, slots=True)
class _Plan:
    """One read: what to read plus the watermark snapshot it is split at."""

    kind: _Kind
    instrument_id: str
    key: int
    interval: Interval | None
    as_of: int | None
    wm: int | None

    @property
    def dataset(self) -> str:
        return "ticks" if self.interval is None else f"bars_{self.interval.value}"


def _empty(kind: _Kind) -> pl.DataFrame:
    return pl.DataFrame(schema={c: _DTYPES.get(c, pl.Float64()) for c in kind.columns})


def tier_for(start_ns: int, end_ns: int, watermark: int | None) -> Tier:
    """Tier that serves ``[start_ns, end_ns)`` given the watermark (``None`` = nothing migrated)."""
    if watermark is None or start_ns >= watermark or start_ns >= end_ns:
        return Tier.HOT
    return Tier.COLD if end_ns <= watermark else Tier.MIXED


def resolve_versions(df: pl.DataFrame, key: Sequence[str]) -> pl.DataFrame:
    """One row per ``key``: the max ``version`` (§7), ordered by ``key``."""
    return (
        df.sort([*key, "version"])
        .unique(subset=list(key), keep="last", maintain_order=True)
        .sort(list(key))
    )


class UnifiedReader:
    """``Reader`` over SQL (hot) and Parquet through DuckDB (cold)."""

    def __init__(
        self,
        engine_or_repo: Engine | sr.SqlRepository,
        duck: DuckSession,
        clock: Clock,
        settings: Settings,
    ) -> None:
        """Inject the SQL engine/repo and DuckDB session; ``clock``/``settings`` are context."""
        self._engine = (
            engine_or_repo.engine
            if isinstance(engine_or_repo, sr.SqlRepository)
            else engine_or_repo
        )
        self._duck = duck
        self.clock = clock
        self.settings = settings

    # -- helpers (blocking; run in a worker thread) -------------------------------------------
    def _key(self, instrument_id: str) -> int:
        q = select(sr.instrument.c.instrument_key).where(
            sr.instrument.c.instrument_id == instrument_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(q).first()
        if row is None:
            raise NotFound(f"unknown instrument {instrument_id}")
        return int(row[0])

    def _watermark(self, kind: _Kind) -> int | None:
        q = select(sr.watermark.c.migrated_until_ns).where(
            sr.watermark.c.table_name == f"md.{kind.table.name}"
        )
        with self._engine.connect() as conn:
            row = conn.execute(q).first()
        return None if row is None else int(row[0])

    def _plan(
        self, kind: _Kind, instrument_id: str, interval: Interval | None, as_of: int | None
    ) -> _Plan:
        """Resolve the instrument (``NotFound``) and snapshot the watermark once per read."""
        return _Plan(
            kind, instrument_id, self._key(instrument_id), interval, as_of, self._watermark(kind)
        )

    def _hot(self, p: _Plan, lo: int, hi: int) -> pl.DataFrame:
        t = p.kind.table
        q = select(*(t.c[c] for c in p.kind.columns)).where(
            t.c.instrument_key == p.key, t.c.ts_ns >= lo, t.c.ts_ns < hi
        )
        if p.interval is not None:
            q = q.where(t.c.interval_code == INTERVAL_CODES[p.interval])
        if p.as_of is not None:
            q = q.where(t.c.ingested_at_ns <= p.as_of)
        with self._engine.connect() as conn:
            rows = [tuple(r) for r in conn.execute(q)]
        floats = {"open", "high", "low", "close", "volume", "price", "size"}
        schema = {c: pl.Float64() if c in floats else pl.Int64() for c in p.kind.columns}
        df = pl.DataFrame(rows, schema=schema, orient="row")
        return df.with_columns(
            pl.col(c).fill_null(float("nan")) for c in ("volume",) if c in df.columns
        )

    def _cold(self, p: _Plan, lo: int, hi: int) -> pl.DataFrame:
        first, last = from_ts_ns(lo), from_ts_ns(hi - 1)
        where = ["ts_ns >= ?", "ts_ns < ?", "year BETWEEN ? AND ?"]
        params: list[object] = [lo, hi, first.year, last.year]
        if first.year == last.year and p.dataset != DAILY_DATASET:
            where.append("month BETWEEN ? AND ?")
            params += [first.month, last.month]
        if p.as_of is not None:
            where.append("ingested_at_ns <= ?")
            params.append(p.as_of)
        sql = (
            f"SELECT {', '.join(p.kind.columns)} FROM read_parquet(?, {_HIVE}) "  # noqa: S608
            f"WHERE {' AND '.join(where)}"
        )
        pattern = self._duck.uri(glob_for(p.dataset, p.instrument_id))
        try:
            tbl = self._duck.query(sql, [pattern, *params])
        except FileNotFoundError:
            return _empty(p.kind)
        out = pl.from_arrow(tbl)
        assert isinstance(out, pl.DataFrame)  # noqa: S101 - narrowing for mypy
        return out

    def _slice(self, p: _Plan, lo: int, hi: int) -> pl.DataFrame:
        """Rows of ``[lo, hi)``: cold below the watermark, hot at/above it, versions resolved."""
        wm = p.wm
        parts: list[pl.DataFrame] = []
        if wm is not None and lo < wm and lo < hi:
            parts.append(self._cold(p, lo, min(hi, wm)))
        if (wm is None or hi > wm) and lo < hi:
            parts.append(self._hot(p, lo if wm is None else max(lo, wm), hi))
        if not parts:
            return _empty(p.kind)
        dtypes = pl.Schema({c: _DTYPES.get(c, pl.Float64()) for c in p.kind.columns})
        return resolve_versions(pl.concat([x.cast(dtypes) for x in parts]), p.kind.key)

    @staticmethod
    def _arrow(df: pl.DataFrame, p: _Plan) -> pa.Table:
        n = df.height
        cols: dict[str, Any] = {}  # arrow arrays of mixed types; pyarrow stubs are invariant
        for f in p.kind.schema:
            arr: Any
            if f.name == "instrument_id":
                arr = pa.array([p.instrument_id] * n).dictionary_encode()
            elif f.name == "interval":
                arr = pa.array([str(p.interval)] * n, pa.string()).dictionary_encode()
            else:
                arr = pa.chunked_array([df.get_column(f.name).to_arrow()]).combine_chunks()
            cols[f.name] = arr.cast(f.type)
        return pa.table(cols, schema=p.kind.schema)

    # -- Reader protocol ----------------------------------------------------------------------
    async def read_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        *,
        as_of_ns: int | None = None,
    ) -> Rows[Bar]:
        """Bars in ``[start_ns, end_ns)``; ``as_of_ns`` selects the version known at that time."""

        def run() -> Rows[Bar]:
            p = self._plan(_BARS, instrument_id, interval, as_of_ns)
            df = self._slice(p, start_ns, end_ns)
            tier = tier_for(start_ns, end_ns, p.wm)
            rows = [
                Bar.model_construct(
                    ts=from_ts_ns(r["ts_ns"]), ts_ns=r["ts_ns"], open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"],
                    volume=None if r["volume"] != r["volume"] else r["volume"],
                    quality=r["quality"], version=r["version"],
                )
                for r in df.iter_rows(named=True)
            ]  # fmt: skip
            return Rows(rows, tier)

        return await asyncio.to_thread(run)

    async def read_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> Rows[Tick]:
        """Ticks in ``[start_ns, end_ns)``."""

        def run() -> Rows[Tick]:
            p = self._plan(_TICKS, instrument_id, None, None)
            df = self._slice(p, start_ns, end_ns)
            tier = tier_for(start_ns, end_ns, p.wm)
            rows = [
                Tick.model_construct(
                    ts=from_ts_ns(r["ts_ns"]), ts_ns=r["ts_ns"], price=r["price"], size=r["size"],
                    side=TickSide(r["side"]), quality=r["quality"], version=r["version"],
                )
                for r in df.iter_rows(named=True)
            ]  # fmt: skip
            return Rows(rows, tier)

        return await asyncio.to_thread(run)

    async def _stream(
        self, kind: _Kind, instrument_id: str, interval: Interval | None, window: tuple[int, int],
        as_of: int | None,
    ) -> RowStream:  # fmt: skip
        start, end = window
        p = await asyncio.to_thread(self._plan, kind, instrument_id, interval, as_of)
        step = _SLICE_NS[p.dataset]

        async def batches() -> AsyncIterator[pa.RecordBatch]:
            lo = start
            while lo < end:
                hi = min(end, lo + step)
                df = await asyncio.to_thread(self._slice, p, lo, hi)
                if df.height:
                    for batch in self._arrow(df, p).to_batches(max_chunksize=_BATCH_ROWS):
                        yield batch
                lo = hi

        return RowStream(tier_for(start, end, p.wm), batches())

    async def stream_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        *,
        as_of_ns: int | None = None,
    ) -> RowStream:
        """Like ``read_bars`` for arrow/parquet output, as ``BAR_V1`` batches."""
        return await self._stream(_BARS, instrument_id, interval, (start_ns, end_ns), as_of_ns)

    async def stream_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> RowStream:
        """Like ``read_ticks`` for arrow/parquet output, as ``TICK_V1`` batches."""
        return await self._stream(_TICKS, instrument_id, None, (start_ns, end_ns), None)

    async def latest(
        self, instrument_ids: Sequence[str], interval: Interval = Interval.D1
    ) -> list[LatestBar]:
        """Last ``interval`` bar of each instrument (hot store only); unknown ids are skipped."""

        def run() -> list[LatestBar]:
            t = sr.bar_hot
            out: list[LatestBar] = []
            for iid in instrument_ids:
                try:
                    key = self._key(iid)
                except NotFound:
                    continue
                q = (
                    select(*(t.c[c] for c in _BARS.columns))
                    .where(t.c.instrument_key == key, t.c.interval_code == INTERVAL_CODES[interval])
                    .order_by(t.c.ts_ns.desc(), t.c.version.desc())
                    .limit(1)
                )
                with self._engine.connect() as conn:
                    row = conn.execute(q).mappings().first()
                if row is None:
                    continue
                vol = row["volume"]
                bar = Bar.model_construct(
                    ts=from_ts_ns(row["ts_ns"]), ts_ns=row["ts_ns"], open=row["open"],
                    high=row["high"], low=row["low"], close=row["close"],
                    volume=None if vol is None else vol, quality=row["quality"],
                    version=row["version"],
                )  # fmt: skip
                out.append(LatestBar(instrument_id=iid, interval=interval, bar=bar))
            return out

        return await asyncio.to_thread(run)

    async def instruments(
        self, *, asset_class: AssetClass | None = None, source: Source | None = None
    ) -> list[Instrument]:
        """Reference data, optionally filtered."""

        def run() -> list[Instrument]:
            q = select(sr.instrument).order_by(sr.instrument.c.instrument_id)
            if asset_class is not None:
                q = q.where(sr.instrument.c.asset_class == asset_class.value)
            if source is not None:
                q = q.where(sr.instrument.c.source == source.value)
            with self._engine.connect() as conn:
                rows = conn.execute(q).mappings().all()
            return [
                Instrument.model_validate({k: v for k, v in r.items() if k != "instrument_key"})
                for r in rows
            ]

        return await asyncio.to_thread(run)

    async def ping(self) -> None:
        """SQL ``SELECT 1`` plus a cold-store reachability check; raises when either is down."""

        def run() -> None:
            with self._engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            self._duck.ping()

        await asyncio.to_thread(run)
