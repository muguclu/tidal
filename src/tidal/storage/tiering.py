"""SQL -> Parquet tiering job (SPEC-03 §2): move rows older than the hot window to Blob.

Scope: ``md.bar_hot`` (every interval) and ``md.tick_hot``. **Every version** of every row below
the cut-off is moved and kept (data-contracts §7). Rows below the watermark that appear in SQL later
(late restatements) are swept the same way into a *new* ``part-`` (the reader merges them, T-208).

Step order (the contract for the crash-injection tests, T-206)
===============================================================
Per table, under ``sp_getapplock('tiering:<table>', Exclusive)`` (second run exits at once):

0. ``target = clock.now - hot_window`` (or its month floor with ``align_months``); the watermark
   ``wm`` is read.
Then, repeated per series (instrument, interval) and per **calendar-month batch** (<= ``batch_rows``
rows, ordered ``ts_ns, [side,] version``), always starting at the series' oldest remaining row:

1. READ    the batch from SQL (all versions, ``ts_ns < target``).
2. PLAN    read the existing cold rows of that range; ``W = batch - cold``. A row is *already cold*
           when key + version + values are equal (``ingested_at_ns`` is ignored: the same bar
           re-ingested with another clock is the same bar). Same key + version with different
           values is a conflict -> :class:`TieringConflict`, nothing is written or deleted.
3. WRITE   ``W`` (if any) as one new immutable ``part-`` via ``parquet_writer``. The part's
           ``tidal.source_hash`` is the sha256 of ``W``.
4. VERIFY  re-read the part (count + sha256 of ``ts_ns``/``close|price`` equal ``W``), then re-read
           the cold range: every batch row must now be present (anti-join empty).
5. TAG     blob index tags of the partition's parts (``age_band`` etc., see ``retag``).
6. MANIFEST rebuild ``_manifest.json`` (rows, min/max ts, sha256 per part). Derived and
           rewritable; it is never the source of truth.
7. DELETE  the batch from SQL in chunks of ``delete_chunk_rows`` (one short transaction each,
           bounded by the chunk's ts range; a chunk that would remove more rows than were read,
           e.g. a concurrent insert, is rolled back and reported).
After the last batch of every series, if nothing failed:

8. WATERMARK advance ``ops.migration_watermark`` to ``max(wm, target)`` in its own transaction.
9. METRICS rows moved / deduped / swept, bytes written, ``sp_spaceused`` before/after.

Crash safety (any process death between or inside the steps)
------------------------------------------------------------
* Nothing is deleted from SQL before the rows are provably in cold storage (4 before 7).
* Parts are created atomically (``put_new``) and never modified, so a crash leaves either no part
  or a complete one. SQL deletes are transactional per chunk.
* Re-running recomputes the plan from the *content* of both tiers: rows already in cold are not
  written again (2), so a crash after 3 yields an empty ``W`` and the run only tags, rebuilds the
  manifest and deletes. A crash during 7 leaves a suffix of the batch in SQL; it is re-planned
  against cold and deleted. A crash after 7 but before 8 leaves the watermark behind, which is
  harmless (the reader reads both tiers in full; the watermark only labels the tier).
* Tags are set before the delete (5 before 7), so there is no state with deleted SQL rows and
  untagged parts. A crash in 6 can lose the manifest; the next run rebuilds it.
* After a crash during 7 the next batch of that series is the remaining suffix (possibly merged
  with following rows), so the *part layout* can differ from an uninterrupted run; the set of
  rows in cold + SQL is identical (compaction, ``compact``, normalises layout).
"""

# ruff: noqa: N818, PLR0917

import hashlib
import io
import json
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from sqlalchemy import ColumnElement, Engine, Table, delete, func, select
from sqlalchemy.engine import Connection

from tidal.common.errors import NotFound, TidalError
from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock, from_ts_ns, month_bounds
from tidal.contracts.schemas import BAR_V1, INTERVAL_CODES, TICK_V1, Interval
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import MAX_PART_ROWS, ObjectStore, write_partition
from tidal.storage.retag import BlobTagger, ensure_tags

log = get_logger(__name__)

DAY_NS: Final = 86_400 * 10**9
DEFAULT_BATCH_ROWS: Final = 1_000_000
DEFAULT_DELETE_CHUNK_ROWS: Final = 50_000
_FETCH_ROWS: Final = 100_000
_DTYPES: Final[dict[str, pl.DataType]] = {
    "ts_ns": pl.Int64(), "ingested_at_ns": pl.Int64(), "quality": pl.Int8(),
    "version": pl.Int16(), "side": pl.Int8(), "trade_count": pl.Int32(), "size": pl.Float32(),
}  # fmt: skip
_BY_CODE: Final = {code: iv for iv, code in INTERVAL_CODES.items()}


class TieringConflict(TidalError):
    """A hot row and a cold row share key and version but differ in value."""

    status = 409
    slug = "tiering-conflict"
    title = "Hot and cold rows conflict"


class TieringVerifyFailed(TidalError):
    """The written Parquet part does not match the SQL batch it was written from."""

    status = 500
    slug = "tiering-verify-failed"
    title = "Tiering verification failed"


class TieringDeleteMismatch(TidalError):
    """A delete chunk would remove rows that were not part of the verified batch."""

    status = 409
    slug = "tiering-delete-mismatch"
    title = "Delete chunk removes unmigrated rows"


@dataclass(frozen=True, slots=True)
class TableSpec:
    """How one hot table maps to Parquet datasets."""

    name: str  # ops.migration_watermark.table_name
    table: Table
    bars: bool
    key: tuple[str, ...]  # versionless key inside (instrument, interval)
    values: tuple[str, ...]  # columns compared to decide "same row"
    checksum_col: str  # (ts_ns, this) is checksummed after writing
    columns: tuple[str, ...]  # SQL columns read, in contract order without identity columns
    schema: pa.Schema

    def dataset(self, interval: Interval | None) -> str:
        """Parquet dataset of ``interval`` (``None`` for ticks)."""
        return "ticks" if interval is None else f"bars_{interval.value}"


BARS: Final = TableSpec(
    "md.bar_hot", sr.bar_hot, True, ("ts_ns",),
    ("open", "high", "low", "close", "volume", "trade_count"), "close",
    ("ts_ns", "open", "high", "low", "close", "volume", "trade_count",
     "quality", "ingested_at_ns", "version"),
    BAR_V1,
)  # fmt: skip
TICKS: Final = TableSpec(
    "md.tick_hot", sr.tick_hot, False, ("ts_ns", "side"), ("price", "size"), "price",
    ("ts_ns", "price", "size", "side", "quality", "ingested_at_ns", "version"), TICK_V1,
)  # fmt: skip
SPECS: Final = {"bar_hot": BARS, "tick_hot": TICKS}
_SPEC_OF_DATASET: Final = {**{f"bars_{i.value}": BARS for i in Interval}, "ticks": TICKS}


def spec_for_dataset(dataset: str) -> TableSpec:
    """Hot table spec that feeds ``dataset``."""
    try:
        return _SPEC_OF_DATASET[dataset]
    except KeyError:
        raise layout.InvalidPath(f"unknown dataset {dataset!r}") from None


@dataclass(frozen=True, slots=True)
class TieringConfig:
    """Job parameters (``hot_window_days`` comes from ``Settings``)."""

    hot_window_days: int
    batch_rows: int = DEFAULT_BATCH_ROWS
    delete_chunk_rows: int = DEFAULT_DELETE_CHUNK_ROWS
    #: Migrate whole calendar months only (``target`` = month floor of the cut-off): one part per
    #: month for daily bars (storage-layout §2) at the price of keeping up to a month more in SQL.
    align_months: bool = False

    def __post_init__(self) -> None:
        """Reject values that would break the batch invariants."""
        if self.hot_window_days <= 0 or self.delete_chunk_rows <= 0:
            raise ValueError("hot_window_days and delete_chunk_rows must be positive")
        if not 0 < self.batch_rows <= MAX_PART_ROWS:
            raise ValueError(f"batch_rows must be in 1..{MAX_PART_ROWS} (one batch = one part)")


@dataclass(slots=True)
class TableResult:
    """Outcome and metrics of one table (SPEC-03 §2 step 8)."""

    table: str
    locked: bool = False
    target_ns: int = 0
    watermark_before: int | None = None
    watermark_after: int | None = None
    batches: int = 0
    rows_moved: int = 0  # rows written to Parquet
    rows_deduped: int = 0  # rows that were already cold (only deleted from SQL)
    rows_swept: int = 0  # moved/deduped rows that lay below the old watermark
    rows_deleted: int = 0
    parts_written: int = 0
    bytes_written: int = 0
    sql_kb_before: int | None = None
    sql_kb_after: int | None = None
    errors: list[str] = field(default_factory=list)


# -- SQL application lock ------------------------------------------------------------------------
_LOCK_SQL: Final = (
    "DECLARE @rc INT; EXEC @rc = sp_getapplock @Resource = ?, "
    "@LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0; SELECT @rc"
)
_UNLOCK_SQL: Final = "EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session'"


@contextmanager
def applock(engine: Engine, resource: str) -> Iterator[bool]:
    """Hold ``sp_getapplock(resource, Exclusive)`` for the block; yields whether it was acquired.

    The lock is session-owned on a dedicated connection and released in ``finally`` (a crash
    closes the connection, which releases it too). Dialects without applocks (the SQLite unit
    tests) always acquire.
    """
    if engine.dialect.name != "mssql":
        yield True
        return
    with engine.connect() as conn:
        rc = conn.exec_driver_sql(_LOCK_SQL, (resource,)).scalar()
        got = rc is not None and int(rc) >= 0
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.exec_driver_sql(_UNLOCK_SQL, (resource,))
                conn.commit()


def space_used_kb(engine: Engine, table: str) -> int | None:
    """``reserved`` KB of ``table`` from ``sp_spaceused`` (``None`` off SQL Server)."""
    if engine.dialect.name != "mssql":
        return None
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "EXEC sp_spaceused @objname = ?, @updateusage = N'TRUE'", (table,)
        ).fetchone()
    return None if row is None else int(str(row[2]).split()[0])


# -- frames, checksums ---------------------------------------------------------------------------
def normalise(df: pl.DataFrame) -> pl.DataFrame:
    """Cast to the contract dtypes and map NULL volume to NaN (data-contracts §3.1)."""
    out = df.with_columns(pl.col(c).cast(t) for c, t in _DTYPES.items() if c in df.columns)
    if "volume" in out.columns:
        out = out.with_columns(pl.col("volume").cast(pl.Float64).fill_null(float("nan")))
    return out


def checksum(ts_ns: np.ndarray, value: np.ndarray) -> str:
    """sha256 over the little-endian bytes of ``ts_ns`` (int64) and ``value`` (float64)."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(ts_ns, dtype="<i8").tobytes())
    h.update(np.ascontiguousarray(value, dtype="<f8").tobytes())
    return h.hexdigest()


def frame_checksum(df: pl.DataFrame, spec: TableSpec) -> str:
    """:func:`checksum` of a frame's ``ts_ns`` and the spec's checksum column."""
    return checksum(df["ts_ns"].to_numpy(), df[spec.checksum_col].to_numpy())


def frame_digest(df: pl.DataFrame) -> str:
    """sha256 over every column of ``df`` (values and null masks); order-sensitive."""
    h = hashlib.sha256()
    for name in df.columns:
        col = df.get_column(name)
        if col.null_count():
            h.update(col.is_null().to_numpy().tobytes())
            col = col.fill_null(0)
        h.update(name.encode())
        h.update(col.to_numpy().tobytes())
    return h.hexdigest()


def _dict_const(value: str, typ: Any, n: int) -> Any:
    index = pa.array(np.zeros(n, dtype=np.int64), pa.int64())
    return pa.DictionaryArray.from_arrays(index, pa.array([value], typ.value_type)).cast(typ)


def to_contract_table(
    df: pl.DataFrame, spec: TableSpec, instrument_id: str, interval: Interval | None
) -> pa.Table:
    """Frame with ``spec.columns`` -> contract table (``BAR_V1`` / ``TICK_V1``) of one series."""
    n = df.height
    cols: dict[str, Any] = {}  # arrow arrays of mixed types; the pyarrow stubs are invariant
    for f in spec.schema:
        if f.name == "instrument_id":
            cols[f.name] = _dict_const(instrument_id, f.type, n)
        elif f.name == "interval":
            cols[f.name] = _dict_const(str(interval), f.type, n)
        else:
            cols[f.name] = df.get_column(f.name).to_arrow().cast(f.type)
    return pa.table(cols, schema=spec.schema)


# -- cold side -----------------------------------------------------------------------------------
def partition_of(dataset: str, ts_ns: int) -> tuple[int, int]:
    """``(year, month)`` of the partition that holds ``ts_ns`` (daily bars: month 0)."""
    moment = from_ts_ns(ts_ns)
    return moment.year, 0 if dataset == layout.DAILY_DATASET else moment.month


def part_paths(store: ObjectStore, dataset: str, iid: str, year: int, month: int) -> list[str]:
    """``part-*.parquet`` paths of a partition in ascending ``seq`` order."""
    prefix = layout.md_partition_prefix(dataset, iid, year, month)
    found: list[tuple[int, str]] = []
    for path in store.list(prefix):
        try:
            part = layout.parse_md_path(path)
        except layout.InvalidPath:
            continue
        if part.seq is not None:
            found.append((part.seq, path))
    return [p for _, p in sorted(found)]


def read_cold(
    store: ObjectStore,
    paths: Sequence[str],
    columns: Sequence[str],
    bounds: tuple[int, int] | None = None,
) -> pl.DataFrame:
    """``columns`` of ``paths`` (in order), optionally only rows in ``[lo, hi)``, with ``_seq``.

    ``_seq`` is the index of the part in ``paths`` so that later parts win on duplicates.
    """
    filters = (
        None
        if bounds is None
        else (pc.field("ts_ns") >= bounds[0]) & (pc.field("ts_ns") < bounds[1])
    )
    frames = []
    for i, path in enumerate(paths):
        table = pq.read_table(io.BytesIO(store.read(path)), columns=list(columns), filters=filters)
        frame = pl.from_arrow(table)
        assert isinstance(frame, pl.DataFrame)  # noqa: S101 - narrowing for mypy
        frames.append(normalise(frame).with_columns(pl.lit(i, pl.Int64).alias("_seq")))
    if not frames:
        schema = {c: _DTYPES.get(c, pl.Float64()) for c in columns}
        return pl.DataFrame(schema=schema).with_columns(pl.lit(0, pl.Int64).alias("_seq"))
    return pl.concat(frames)


def plan_write(rows: pl.DataFrame, cold: pl.DataFrame, spec: TableSpec) -> pl.DataFrame:
    """Rows of ``rows`` that are not yet cold; raise :class:`TieringConflict` on a mismatch.

    Identity is key + version; equal values (NaN equals NaN, NULL equals NULL) mean "already cold".
    """
    if cold.height == 0:
        return rows
    on = [*spec.key, "version"]
    have = (
        cold.unique(subset=on, keep="last", maintain_order=True)
        .select([*on, *spec.values])
        .rename({v: f"{v}__cold" for v in spec.values})
        .with_columns(pl.lit(True).alias("__present"))
    )
    joined = rows.join(have, on=on, how="left", maintain_order="left")
    present = pl.col("__present").fill_null(False)
    same = pl.all_horizontal([pl.col(v).eq_missing(pl.col(f"{v}__cold")) for v in spec.values])
    if joined.filter(present & ~same).height:
        raise TieringConflict(
            "a hot row has the same key and version as a cold row with other values"
        )
    return joined.filter(~present).select(rows.columns)


def _entry(path: str, data: bytes) -> dict[str, Any]:  # JSON-shaped
    pf = pq.ParquetFile(io.BytesIO(data))
    md = pf.metadata
    lows: list[int] = []
    highs: list[int] = []
    for i in range(md.num_row_groups):
        stats = md.row_group(i).column(0).statistics  # ts_ns is column 0 in every schema
        if stats is not None and stats.has_min_max:
            lows.append(cast(int, stats.min))
            highs.append(cast(int, stats.max))
    meta = pf.schema_arrow.metadata or {}
    return {
        "name": path.rsplit("/", 1)[1],
        "rows": md.num_rows,
        "min_ts_ns": min(lows) if lows else None,
        "max_ts_ns": max(highs) if highs else None,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source_hash": meta.get(b"tidal.source_hash", b"").decode(),
    }


def update_manifest(
    store: ObjectStore,
    dataset: str,
    iid: str,
    year: int,
    month: int,
    known: Mapping[str, bytes] | None = None,
) -> None:
    """Rebuild the partition's ``_manifest.json`` from its parts (derived, deterministic).

    Entries of unchanged parts are reused from the old manifest; ``known`` supplies bytes just
    written so they are not downloaded again. The file is replaced (delete + create-only put); a
    crash in between loses only this derived file, and the next run rebuilds it.
    """
    mpath = layout.md_manifest_path(dataset, iid, year, month)
    old_bytes: bytes | None = None
    old: dict[str, dict[str, Any]] = {}  # parsed JSON
    try:
        raw = store.read(mpath)
        old = {e["name"]: e for e in json.loads(raw).get("parts", [])}
        old_bytes = raw
    except (NotFound, ValueError, KeyError, TypeError):
        old = {}
    known = known or {}
    entries: list[dict[str, Any]] = []
    for path in part_paths(store, dataset, iid, year, month):
        name = path.rsplit("/", 1)[1]
        if name in old and path not in known:
            entries.append(old[name])
        else:
            entries.append(_entry(path, known[path] if path in known else store.read(path)))
    lows = [e["min_ts_ns"] for e in entries if e["min_ts_ns"] is not None]
    highs = [e["max_ts_ns"] for e in entries if e["max_ts_ns"] is not None]
    body = {
        "dataset": dataset,
        "instrument_id": iid,
        "year": year,
        "month": month,
        "rows": sum(int(e["rows"]) for e in entries),
        "min_ts_ns": min(lows) if lows else None,
        "max_ts_ns": max(highs) if highs else None,
        "parts": entries,
    }
    text = (json.dumps(body, sort_keys=True, indent=1) + "\n").encode()
    if old_bytes == text:
        return
    with tempfile.TemporaryDirectory(prefix="tidal-manifest-") as tmp:
        local = Path(tmp) / "_manifest.json"
        local.write_bytes(text)
        store.delete(mpath)
        store.put_new(mpath, local)


def tag_partition(
    store: ObjectStore,
    tagger: BlobTagger,
    now_year: int,
    dataset: str,
    iid: str,
    year: int,
    month: int,
) -> int:
    """Ensure the tags of every part of a partition; returns the number of tag writes."""
    return sum(
        ensure_tags(tagger, p, now_year) for p in part_paths(store, dataset, iid, year, month)
    )


# -- hot side ------------------------------------------------------------------------------------
def _fetch(conn: Connection, query: object, spec: TableSpec) -> pl.DataFrame:
    """Run ``query`` in bounded fetches and return one normalised frame."""
    schema = {
        c: pl.Float64() if c in {"open", "high", "low", "close", "volume", "price", "size"}
        else pl.Int64()
        for c in spec.columns
    }  # fmt: skip
    frames = [
        pl.DataFrame([tuple(r) for r in chunk], schema=schema, orient="row")
        for chunk in conn.execution_options(stream_results=True)
        .execute(query)  # type: ignore[call-overload]
        .partitions(_FETCH_ROWS)
    ]
    if not frames:
        return normalise(pl.DataFrame(schema=schema))
    return normalise(pl.concat(frames))


def _series_conds(spec: TableSpec, key: int, code: int) -> list[ColumnElement[bool]]:
    t = spec.table
    conds: list[ColumnElement[bool]] = [t.c.instrument_key == key]
    if spec.bars:
        conds.append(t.c.interval_code == code)
    return conds


def list_series(engine: Engine, spec: TableSpec, target: int) -> list[tuple[int, int]]:
    """``(instrument_key, interval_code)`` pairs with rows below ``target`` (ticks: code 0)."""
    t = spec.table
    cols = [t.c.instrument_key, *([t.c.interval_code] if spec.bars else [])]
    q = select(*cols).where(t.c.ts_ns < target).distinct().order_by(*cols)
    with engine.connect() as conn:
        return [(int(r[0]), int(r[1]) if spec.bars else 0) for r in conn.execute(q)]


def next_batch(
    engine: Engine, spec: TableSpec, key: int, code: int, target: int, batch_rows: int
) -> pl.DataFrame:
    """Oldest remaining batch of a series: one calendar month, all versions, <= ``batch_rows``."""
    t = spec.table
    conds = _series_conds(spec, key, code)
    with engine.connect() as conn:
        lo = conn.execute(select(func.min(t.c.ts_ns)).where(*conds, t.c.ts_ns < target)).scalar()
        if lo is None:
            return normalise(pl.DataFrame(schema={c: pl.Int64() for c in spec.columns}))
        moment = from_ts_ns(int(lo))
        hi = min(month_bounds(moment.year, moment.month)[1], target)
        order = [t.c[c] for c in (*spec.key, "version")]
        q = (
            select(*(t.c[c] for c in spec.columns))
            .where(*conds, t.c.ts_ns < hi)
            .order_by(*order)
            .limit(batch_rows)
        )
        df = _fetch(conn, q, spec)
    if df.height == batch_rows and df["ts_ns"][0] != df["ts_ns"][-1]:
        df = df.filter(pl.col("ts_ns") < df["ts_ns"][-1])  # never split one timestamp
    return df


def chunk_bounds(ts: np.ndarray, size: int) -> list[tuple[int, int]]:
    """Index ranges of about ``size`` rows that never split a run of equal timestamps."""
    n, i, out = len(ts), 0, []
    while i < n:
        j = min(i + size, n)
        if j < n:
            j = int(np.searchsorted(ts, ts[j - 1], side="right"))
        out.append((i, j))
        i = j
    return out


def delete_batch(
    engine: Engine, spec: TableSpec, key: int, code: int, df: pl.DataFrame, chunk_rows: int
) -> int:
    """Delete exactly the rows of ``df`` from SQL, one short transaction per chunk."""
    t = spec.table
    ts = df["ts_ns"].to_numpy()
    deleted = 0
    for a, b in chunk_bounds(ts, chunk_rows):
        with engine.begin() as conn:
            res = conn.execute(
                delete(t).where(
                    *_series_conds(spec, key, code),
                    t.c.ts_ns >= int(ts[a]),
                    t.c.ts_ns <= int(ts[b - 1]),
                )
            )
            if res.rowcount > b - a:  # rollback on exit: those rows were not verified as cold
                raise TieringDeleteMismatch("delete chunk matched rows that were not migrated")
            deleted += res.rowcount
    return deleted


# -- the job -------------------------------------------------------------------------------------
def _migrate_batch(
    store: ObjectStore,
    tagger: BlobTagger,
    engine: Engine,
    spec: TableSpec,
    series: tuple[int, int, str, Interval | None],
    df: pl.DataFrame,
    cfg: TieringConfig,
    now_year: int,
    swept_below: int | None,
    res: TableResult,
) -> None:
    key, code, iid, interval = series
    dataset = spec.dataset(interval)
    year, month = partition_of(dataset, int(df["ts_ns"][0]))
    bounds = (int(df["ts_ns"][0]), int(df["ts_ns"][-1]) + 1)
    cols = [*spec.key, "version", *spec.values]

    cold = read_cold(store, part_paths(store, dataset, iid, year, month), cols, bounds)  # 2
    todo = plan_write(df, cold, spec)
    known: dict[str, bytes] = {}
    if todo.height:
        paths = write_partition(  # 3
            store, dataset=dataset, instrument_id=iid, year=year, month=month,
            batches=[to_contract_table(todo, spec, iid, interval)],
            source_hash=frame_digest(todo),
        )  # fmt: skip
        got_rows, want = 0, frame_checksum(todo, spec)
        ts_parts, px_parts = [], []
        for path in paths:  # 4a: the written bytes equal the SQL batch
            data = store.read(path)
            known[path] = data
            tbl = pq.read_table(io.BytesIO(data), columns=["ts_ns", spec.checksum_col])
            got_rows += tbl.num_rows
            ts_parts.append(tbl.column("ts_ns").to_numpy())
            px_parts.append(tbl.column(spec.checksum_col).to_numpy())
        if (
            got_rows != todo.height
            or checksum(np.concatenate(ts_parts), np.concatenate(px_parts)) != want
        ):
            raise TieringVerifyFailed(f"written part for {dataset}/{iid} differs from SQL batch")
        after = read_cold(store, part_paths(store, dataset, iid, year, month), cols, bounds)
        if plan_write(df, after, spec).height:  # 4b: every batch row is cold now
            raise TieringVerifyFailed(f"batch rows missing in cold after write for {iid}")
        res.parts_written += len(paths)
        res.bytes_written += sum(len(b) for b in known.values())
    tag_partition(store, tagger, now_year, dataset, iid, year, month)  # 5
    update_manifest(store, dataset, iid, year, month, known)  # 6
    deleted = delete_batch(engine, spec, key, code, df, cfg.delete_chunk_rows)  # 7
    res.batches += 1
    res.rows_moved += todo.height
    res.rows_deduped += df.height - todo.height
    res.rows_deleted += deleted
    if swept_below is not None:
        res.rows_swept += int((df["ts_ns"] < swept_below).sum())


def run_table(
    engine: Engine,
    store: ObjectStore,
    tagger: BlobTagger,
    clock: Clock,
    cfg: TieringConfig,
    spec: TableSpec,
) -> TableResult:
    """Migrate one hot table (step order in the module docstring); never raises on data errors.

    Data problems (:class:`TidalError`) stop the affected series, are listed in
    ``TableResult.errors`` and keep the watermark where it is.
    """
    res = TableResult(spec.name)
    with applock(engine, f"tiering:{spec.name}") as got:
        if not got:
            res.locked = True
            log.info("tiering_skipped_locked", table=spec.name)
            return res
        repo = sr.SqlRepository(engine, clock)
        wm = repo.get_watermark(spec.name)
        target = clock.now_ns() - cfg.hot_window_days * DAY_NS
        if cfg.align_months:
            moment = from_ts_ns(target)
            target = month_bounds(moment.year, moment.month)[0]
        res.target_ns, res.watermark_before = target, wm
        res.sql_kb_before = space_used_kb(engine, spec.name)
        ids = {v: k for k, v in repo.instrument_keys().items()}
        now_year = clock.now().year
        for key, code in list_series(engine, spec, target):
            series = (key, code, ids[key], _BY_CODE[code] if spec.bars else None)
            try:
                _drain_series(engine, store, tagger, spec, series, target, cfg, now_year, wm, res)
            except TidalError as exc:
                res.errors.append(f"{series[2]}: {exc.slug}")
                log.error("tiering_series_failed", table=spec.name, instrument=series[2],
                          error=exc.slug)  # fmt: skip
        if not res.errors:
            res.watermark_after = target if wm is None else max(wm, target)
            repo.set_watermark(spec.name, res.watermark_after)  # 8
        else:
            res.watermark_after = wm
        res.sql_kb_after = space_used_kb(engine, spec.name)
    log.info("tiering_table_done", table=spec.name, rows_moved=res.rows_moved,
             rows_deduped=res.rows_deduped, rows_swept=res.rows_swept,
             rows_deleted=res.rows_deleted,
             bytes_written=res.bytes_written, parts_written=res.parts_written,
             sql_kb_before=res.sql_kb_before, sql_kb_after=res.sql_kb_after,
             watermark=res.watermark_after, errors=len(res.errors))  # fmt: skip
    return res


def _drain_series(
    engine: Engine,
    store: ObjectStore,
    tagger: BlobTagger,
    spec: TableSpec,
    series: tuple[int, int, str, Interval | None],
    target: int,
    cfg: TieringConfig,
    now_year: int,
    wm: int | None,
    res: TableResult,
) -> None:
    key, code = series[0], series[1]
    prev: tuple[int, int] | None = None
    while True:
        df = next_batch(engine, spec, key, code, target, cfg.batch_rows)
        if df.height == 0:
            return
        here = (int(df["ts_ns"][0]), df.height)
        if here == prev:  # the previous batch is still in SQL: never loop forever
            raise TieringDeleteMismatch("no progress: the batch was not removed from SQL")
        prev = here
        _migrate_batch(store, tagger, engine, spec, series, df, cfg, now_year, wm, res)


def run_tiering(
    engine: Engine,
    store: ObjectStore,
    tagger: BlobTagger,
    clock: Clock,
    cfg: TieringConfig,
    tables: Sequence[str] = ("bar_hot", "tick_hot"),
) -> list[TableResult]:
    """Run the migration for ``tables`` (names in :data:`SPECS`) and return one result each."""
    return [run_table(engine, store, tagger, clock, cfg, SPECS[name]) for name in tables]
