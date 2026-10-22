"""Hot/cold router (SPEC-01 §2): ``ts_ns >= clock.now() - hot_window`` -> SQL, else Parquet.

Hot rows go through ``SqlRepository.upsert_*`` (which owns versioning and no-op detection). Cold
rows go through ``write_partition`` into the partition of their trade time (year for ``1d`` bars
with ``month=00``, month otherwise). Cold parts are immutable, so with ``dedupe_cold`` the router
reads the existing parts of the partition first: unchanged bars are skipped (0 rows written) and
a changed bar is appended as a new part with ``version = latest + 1`` and ``RESTATED`` set.
Instrument bounds (``first_ts_ns``/``last_ts_ns``) are tracked from every routed daily bar and
tick (written or not) and persisted by :meth:`Router.flush_bounds`.
"""

import io
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from sqlalchemy import select

from tidal.common.timeutil import Clock
from tidal.contracts.instruments import CATALOGUE_V1
from tidal.contracts.schemas import BAR_V1, QualityFlag
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import ObjectStore, write_partition

DAY_NS: Final = 86_400 * 10**9
_VALUES: Final = ("open", "high", "low", "close", "volume", "trade_count")


@dataclass(frozen=True, slots=True)
class Routed:
    """Rows actually written to each tier."""

    hot: int = 0
    cold: int = 0

    def __add__(self, other: "Routed") -> "Routed":
        """Sum two results."""
        return Routed(self.hot + other.hot, self.cold + other.cold)


def read_part(store: ObjectStore, path: str, columns: list[str] | None = None) -> pa.Table:
    """Read (selected ``columns`` of) one part through ``store.read``."""
    return pq.read_table(io.BytesIO(store.read(path)), columns=columns)


def _plan_cold(incoming: pa.Table, existing: pa.Table | None) -> pa.Table:
    """Rows of ``incoming`` that are new (v1) or changed (latest+1, ``RESTATED``)."""
    if existing is None or existing.num_rows == 0:
        return incoming
    new = pl.from_arrow(incoming.select(["ts_ns", *_VALUES]))
    old = pl.from_arrow(existing.select(["ts_ns", "version", *_VALUES]))
    assert isinstance(new, pl.DataFrame)  # noqa: S101 - narrowing
    assert isinstance(old, pl.DataFrame)  # noqa: S101 - narrowing
    old = old.sort("version").group_by("ts_ns").last().rename({c: f"{c}_old" for c in _VALUES})
    joined = new.join(old, on="ts_ns", how="left", maintain_order="left")
    is_new = joined["version"].is_null().to_numpy()
    changed = joined.select(
        pl.any_horizontal(*(pl.col(c).ne_missing(pl.col(f"{c}_old")) for c in _VALUES))
    ).to_series()
    version = np.where(is_new, 1, joined["version"].fill_null(0).to_numpy() + 1)
    quality = incoming.column("quality").to_numpy() | np.where(is_new, 0, int(QualityFlag.RESTATED))
    out = incoming.set_column(
        incoming.schema.get_field_index("version"),
        BAR_V1.field("version"),
        pa.array(version.astype(np.int16)),
    ).set_column(
        incoming.schema.get_field_index("quality"),
        BAR_V1.field("quality"),
        pa.array(quality.astype(np.int8)),
    )
    return out.filter(pa.array(is_new | changed.to_numpy()))


class Router:
    """Splits tables by age and writes each part to its tier; clock and stores are injected."""

    def __init__(
        self,
        repo: sr.SqlRepository,
        store: ObjectStore,
        clock: Clock,
        hot_window_days: int,
        *,
        dedupe_cold: bool = False,
    ) -> None:
        """``dedupe_cold`` reads existing cold parts to skip unchanged / restate changed bars."""
        self.repo, self.store, self.clock = repo, store, clock
        self.hot_window_days = hot_window_days
        self.dedupe_cold = dedupe_cold
        self.bounds: dict[str, tuple[int, int]] = {}

    @property
    def cutoff_ns(self) -> int:
        """Rows with ``ts_ns >= cutoff_ns`` are hot."""
        return self.clock.now_ns() - self.hot_window_days * DAY_NS

    # -- reference data ------------------------------------------------------------------------
    def _db_bounds(self) -> dict[str, tuple[int | None, int | None]]:
        c = sr.instrument.c
        with self.repo.engine.connect() as conn:
            rows = conn.execute(select(c.instrument_id, c.first_ts_ns, c.last_ts_ns))
            return {r[0]: (r[1], r[2]) for r in rows}

    def _rows(
        self, ids: Iterable[str], bounds: Mapping[str, tuple[int, int]]
    ) -> list[dict[str, object]]:
        """Catalogue rows for ``ids`` with stored bounds widened by ``bounds``."""
        wanted, have = set(ids), self._db_bounds()
        rows: list[dict[str, object]] = []
        for entry in CATALOGUE_V1:
            if entry.instrument_id not in wanted:
                continue
            lo, hi = have.get(entry.instrument_id, (None, None))
            new = bounds.get(entry.instrument_id)
            if new is not None:
                lo = new[0] if lo is None else min(lo, new[0])
                hi = new[1] if hi is None else max(hi, new[1])
            row: dict[str, object] = entry.to_instrument().model_dump(mode="json")
            row["first_ts_ns"], row["last_ts_ns"] = lo, hi
            rows.append(row)
        return rows

    def ensure_instruments(self, instrument_ids: Iterable[str]) -> dict[str, int]:
        """Upsert catalogue rows into ``ref.instrument`` (keeping stored bounds); returns keys."""
        return self.repo.upsert_instruments(self._rows(instrument_ids, {}))

    def note_bounds(self, bounds: Mapping[str, tuple[int, int]]) -> None:
        """Widen the tracked ``(first, last)`` ts of instruments (also merges worker results)."""
        for iid, (lo, hi) in bounds.items():
            old = self.bounds.get(iid)
            self.bounds[iid] = (lo, hi) if old is None else (min(old[0], lo), max(old[1], hi))

    def flush_bounds(self) -> None:
        """Persist tracked bounds to ``ref.instrument`` (widening, never narrowing)."""
        if self.bounds:
            self.repo.upsert_instruments(self._rows(self.bounds, self.bounds))

    # -- routing -------------------------------------------------------------------------------
    def split(self, table: pa.Table) -> tuple[pa.Table, pa.Table]:
        """``(hot, cold)`` halves of ``table`` by ``cutoff_ns``."""
        is_hot = pc.greater_equal(table.column("ts_ns"), pa.scalar(self.cutoff_ns, pa.int64()))
        return table.filter(is_hot), table.filter(pc.invert(is_hot))

    def _track(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        df = pl.from_arrow(table.select(["ts_ns", "instrument_id"]))
        assert isinstance(df, pl.DataFrame)  # noqa: S101 - narrowing
        agg = (
            df.with_columns(pl.col("instrument_id").cast(pl.String))
            .group_by("instrument_id")
            .agg(pl.col("ts_ns").min().alias("lo"), pl.col("ts_ns").max().alias("hi"))
        )
        self.note_bounds({r[0]: (r[1], r[2]) for r in agg.iter_rows()})

    def route_bars(self, bars: pa.Table, source_hash: str) -> Routed:
        """Write ``bars``: hot rows to SQL, cold rows to Parquet; returns the rows written."""
        hot, cold = self.split(bars)
        if bars.num_rows and bars.column("interval")[0].as_py() == "1d":
            self._track(bars)
        n_hot = self.repo.upsert_bars(hot) if hot.num_rows else 0
        return Routed(n_hot, self._write_cold_bars(cold, source_hash))

    def _write_cold_bars(self, cold: pa.Table, source_hash: str) -> int:
        if cold.num_rows == 0:
            return 0
        df = pl.from_arrow(cold.select(["ts_ns", "instrument_id", "interval"]))
        assert isinstance(df, pl.DataFrame)  # noqa: S101 - narrowing
        ts = pl.col("ts_ns").cast(pl.Datetime("ns"))
        keys = df.with_row_index("i").with_columns(
            pl.col("instrument_id").cast(pl.String),
            pl.col("interval").cast(pl.String),
            ts.dt.year().alias("year"),
            ts.dt.month().alias("month"),
        )
        keys = keys.with_columns(
            pl.when(pl.col("interval") == "1d").then(0).otherwise(pl.col("month")).alias("month")
        )
        written = 0
        for _, grp in keys.group_by(
            ["instrument_id", "interval", "year", "month"], maintain_order=True
        ):
            iid, interval = grp["instrument_id"][0], grp["interval"][0]
            year, month = int(grp["year"][0]), int(grp["month"][0])
            part = cold.take(grp["i"].to_arrow())
            dataset = f"bars_{interval}"
            if self.dedupe_cold:
                prefix = layout.md_partition_prefix(dataset, iid, year, month)
                parts = [
                    read_part(self.store, p, ["ts_ns", "version", *_VALUES])
                    for p in self.store.list_paths(prefix)
                    if p.endswith(".parquet")
                ]
                part = _plan_cold(part, pa.concat_tables(parts) if parts else None)
            if part.num_rows:
                write_partition(
                    self.store,
                    dataset=dataset,
                    instrument_id=iid,
                    year=year,
                    month=month,
                    batches=[part],
                    source_hash=source_hash,
                )
                written += part.num_rows
        return written

    def write_stream(
        self,
        dataset: str,
        *,
        instrument_id: str,
        year: int,
        month: int,
        batches: Iterable[pa.Table],
        source_hash: str,
    ) -> Routed:
        """Stream sorted ``batches`` of one partition: hot rows to SQL, cold rows to new parts."""
        counts = {"hot": 0, "cold": 0}

        def cold_only() -> Iterator[pa.Table]:
            for batch in batches:
                if dataset == "ticks" or batch.column("interval")[0].as_py() == "1d":
                    self._track(batch)
                hot, cold = self.split(batch)
                if hot.num_rows:
                    upsert = self.repo.upsert_ticks if dataset == "ticks" else self.repo.upsert_bars
                    counts["hot"] += upsert(hot)
                if cold.num_rows:
                    counts["cold"] += cold.num_rows
                    yield cold

        write_partition(
            self.store,
            dataset=dataset,
            instrument_id=instrument_id,
            year=year,
            month=month,
            batches=cold_only(),
            source_hash=source_hash,
        )
        return Routed(counts["hot"], counts["cold"])
