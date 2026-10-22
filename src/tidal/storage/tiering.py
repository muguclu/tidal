"""SQL -> Parquet tiering job (SPEC-03 §2): move rows older than the hot window to Blob.

Scope: ``md.bar_hot`` (every interval) and ``md.tick_hot``. **Every version** of every row below
the target is moved and kept (data-contracts §7). ``target`` is the hot-window cut-off **rounded
down to a month start**, so each batch is one whole calendar month (one part per month, daily bars
append to their yearly partition; storage-layout §2). There is no lower bound: rows that reach SQL
below the watermark later (late restatements) are swept into a *new* ``part-`` (the reader merges
them, T-208).

Step order (the contract for the crash-injection tests, T-206)
===============================================================
Per table, under ``sp_getapplock('tiering:<table>', Exclusive)`` (a second run exits at once; the
lock is re-checked before every delete chunk):

0. ``target = month_floor(clock.now - hot_window)``; the watermark ``wm`` is read.
Then, per series (instrument, interval) and per calendar-month window, oldest first:

1. READ    the month from SQL in pages of ``batch_rows`` (all versions, ``ts_ns < target``), never
           splitting one timestamp. Memory is one page, not one month.
2. PLAN    per page ``W = page - cold`` where *cold* is read once per month from the parts whose
           manifest min/max overlaps the month (:func:`plan_write`):
           * a hot row is *already cold* when cold has the same key + version + values with
             ``cold.ingested_at_ns <= hot.ingested_at_ns``; a cold copy ingested **later** does not
             count (as-of reads would lose the earlier version), the hot row is written too;
           * same key + version, other values, hot newer than every cold row of the key: the hot
             rows of the key are **re-versioned** to ``max(cold, hot version) + 1 ..`` with
             ``RESTATED`` (ADR-0014; same rule as ``ingest.router``); a row whose re-versioned
             copy is already cold is recognised by key + values + ``ingested_at_ns``;
           * any other same key + version mismatch -> :class:`TieringConflict`, nothing deleted.
3. WRITE   all of ``W`` in **one** ``write_partition`` call (it rolls at ``MAX_PART_ROWS``).
4. VERIFY  re-read the written parts: row count and sha256 of ``ts_ns``/``close|price`` equal ``W``.
           On a mismatch the parts just written are deleted before :class:`TieringVerifyFailed`.
5. TAG     blob index tags of the partition's parts (``age_band`` etc., see ``retag``).
6. MANIFEST rebuild ``_manifest.json`` (rows, min/max ts, sha256 per part): derived, replaceable,
           never read by the reader.
7. DELETE  the month from SQL in chunks of ``delete_chunk_rows``, one short transaction each,
           bounded by the chunk's ts range; a chunk that would remove more rows than were read
           (a concurrent insert) is rolled back and reported.
After the last window of every series, if nothing failed:

8. WATERMARK advance ``ops.migration_watermark`` to ``max(wm, target)`` in its own transaction.
9. METRICS rows moved / deduped / swept, bytes written, ``sp_spaceused`` before/after.

``dry_run`` runs 1-2 only and reports what would move.

Crash safety (process death between or inside any step)
-------------------------------------------------------
* Nothing is deleted from SQL before the rows are provably in cold storage (4 before 7).
* Parts are created atomically (``put_new``) and never modified: a crash leaves no part or a
  complete one. SQL deletes are transactional per chunk.
* Idempotency is by *content*, not by a marker: a re-run recomputes the plan against what cold
  really holds, so rows already written produce an empty ``W`` and the run only tags, rebuilds the
  manifest and deletes. A crash between delete chunks leaves a suffix of the month in SQL which is
  planned against cold and deleted; no new part is written, so the layout equals an uninterrupted
  run. A crash after 7 but before 8 leaves the watermark behind, which is harmless (the reader
  reads both tiers in full; the watermark only labels the tier).
* Tags are set before the delete, so there is never an untagged part whose SQL rows are gone. A
  crash inside 6 (between the manifest delete and the put) loses only the derived manifest; the
  next run rebuilds it.
"""

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
from sqlalchemy.exc import DBAPIError

from tidal.common.errors import NotFound, TidalError
from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock, from_ts_ns, month_bounds
from tidal.contracts.messages import TieringJob
from tidal.contracts.schemas import BAR_V1, INTERVAL_CODES, TICK_V1, Interval, QualityFlag
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import (
    AzureBlobStore,
    LocalStore,
    ObjectStore,
    write_partition,
)
from tidal.storage.retag import BlobTagger, ensure_tags

log = get_logger(__name__)

DAY_NS: Final = 86_400 * 10**9
DEFAULT_BATCH_ROWS: Final = 500_000
DEFAULT_DELETE_CHUNK_ROWS: Final = 50_000
_FETCH_ROWS: Final = 100_000
_DTYPES: Final[dict[str, pl.DataType]] = {
    "ts_ns": pl.Int64(), "ingested_at_ns": pl.Int64(), "quality": pl.Int8(),
    "version": pl.Int16(), "side": pl.Int8(), "trade_count": pl.Int32(), "size": pl.Float32(),
}  # fmt: skip
_BY_CODE: Final = {code: iv for iv, code in INTERVAL_CODES.items()}


class TieringConflict(TidalError):  # noqa: N818 - names follow the problem slugs
    """Hot and cold rows share key and version with other values and cannot be re-versioned."""

    status = 409
    slug = "tiering-conflict"
    title = "Hot and cold rows conflict"


class TieringVerifyFailed(TidalError):  # noqa: N818
    """The written Parquet parts do not match the SQL rows they were written from."""

    status = 500
    slug = "tiering-verify-failed"
    title = "Tiering verification failed"


class TieringDeleteMismatch(TidalError):  # noqa: N818
    """A delete chunk would remove rows that were not part of the verified month."""

    status = 409
    slug = "tiering-delete-mismatch"
    title = "Delete chunk removes unmigrated rows"


class LockLost(TidalError):  # noqa: N818
    """The application lock is no longer held by the job's session."""

    status = 409
    slug = "tiering-lock-lost"
    title = "Tiering lock lost"


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


def spec_for_table(name: str) -> TableSpec:
    """Spec of ``bar_hot`` / ``tick_hot`` or the contract names ``md.bar_hot`` / ``md.tick_hot``."""
    try:
        return SPECS[name.removeprefix("md.")]
    except KeyError:
        raise layout.InvalidPath(f"unknown table {name!r}") from None


@dataclass(frozen=True, slots=True)
class TieringConfig:
    """Job parameters (``hot_window_days``: ``Settings`` or the ``tiering.job`` message)."""

    hot_window_days: int
    batch_rows: int = DEFAULT_BATCH_ROWS  # rows per SQL read page (memory bound)
    delete_chunk_rows: int = DEFAULT_DELETE_CHUNK_ROWS
    dry_run: bool = False

    def __post_init__(self) -> None:
        """Reject values that would break the paging invariants."""
        if self.hot_window_days <= 0 or self.delete_chunk_rows <= 0 or self.batch_rows <= 0:
            raise ValueError("hot_window_days, batch_rows and delete_chunk_rows must be positive")


@dataclass(slots=True)
class TableResult:
    """Outcome and metrics of one table (SPEC-03 §2 step 9)."""

    table: str
    locked: bool = False
    dry_run: bool = False
    target_ns: int = 0
    watermark_before: int | None = None
    watermark_after: int | None = None
    batches: int = 0
    rows_moved: int = 0  # rows written to Parquet (dry run: rows that would be)
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
_MODE_SQL: Final = "SELECT APPLOCK_MODE('public', ?, 'Session')"


class AppLock:
    """Result of :func:`applock`: truthy when acquired; :meth:`check` proves it is still held."""

    def __init__(self, conn: Connection | None, resource: str, held: bool) -> None:
        """``conn`` is the session that owns the lock (``None`` where applocks do not exist)."""
        self.conn, self.resource, self.held = conn, resource, held

    def __bool__(self) -> bool:
        """Whether the lock was acquired."""
        return self.held

    def check(self) -> None:
        """Raise :class:`LockLost` unless the owning session still holds the lock exclusively.

        ``APPLOCK_MODE`` only reports on the *calling* session, so it runs on the lock connection.
        """
        if self.conn is None or not self.held:
            return
        try:
            mode = self.conn.exec_driver_sql(_MODE_SQL, (self.resource,)).scalar()
            self.conn.commit()
        except Exception as exc:  # dead connection, any driver error
            raise LockLost("could not verify the tiering application lock") from exc
        if mode != "Exclusive":
            raise LockLost("the tiering application lock is no longer held")


@contextmanager
def applock(engine: Engine, resource: str) -> Iterator[AppLock]:
    """Hold ``sp_getapplock(resource, Exclusive)`` for the block; the yielded lock is truthy if got.

    Session-owned on a dedicated connection and released in ``finally``; a failing release (dead
    connection) is logged and never masks the exception that ended the block. Dialects without
    applocks (the SQLite unit tests) always acquire.
    """
    if engine.dialect.name != "mssql":
        yield AppLock(None, resource, True)
        return
    with engine.connect() as conn:
        rc = conn.exec_driver_sql(_LOCK_SQL, (resource,)).scalar()
        got = rc is not None and int(rc) >= 0
        conn.commit()
        try:
            yield AppLock(conn, resource, got)
        finally:
            if got:
                try:
                    conn.exec_driver_sql(_UNLOCK_SQL, (resource,))
                    conn.commit()
                except Exception:
                    log.warning("applock_release_failed", resource=resource)


def space_used_kb(engine: Engine, table: str) -> int | None:
    """``reserved`` KB of ``table`` (``sp_spaceused``); ``None`` off SQL Server or without rights.

    Runs without ``@updateusage`` (it needs more privilege and scans the table), so the number can
    lag behind ghost cleanup.
    """
    if engine.dialect.name != "mssql":
        return None
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql("EXEC sp_spaceused @objname = ?", (table,)).fetchone()
    except DBAPIError:
        log.warning("sp_spaceused_unavailable", table=table)
        return None
    return None if row is None else int(str(row[2]).split()[0])


# -- frames, checksums ---------------------------------------------------------------------------
def normalise(df: pl.DataFrame) -> pl.DataFrame:
    """Cast to the contract dtypes and map NULL volume to NaN (data-contracts §3.1)."""
    out = df.with_columns(pl.col(c).cast(t) for c, t in _DTYPES.items() if c in df.columns)
    if "volume" in out.columns:
        out = out.with_columns(pl.col("volume").cast(pl.Float64).fill_null(float("nan")))
    return out


class RunningChecksum:
    """sha256 over the little-endian ``ts_ns`` (int64) and value (float64) streams of chunks."""

    def __init__(self) -> None:
        """Start empty."""
        self._ts, self._val, self.rows = hashlib.sha256(), hashlib.sha256(), 0

    def update(self, ts_ns: np.ndarray, value: np.ndarray) -> None:
        """Add one chunk (same order on both sides of a comparison)."""
        self._ts.update(np.ascontiguousarray(ts_ns, dtype="<i8").tobytes())
        self._val.update(np.ascontiguousarray(value, dtype="<f8").tobytes())
        self.rows += len(ts_ns)

    def hexdigest(self) -> str:
        """Digest of everything added so far."""
        return hashlib.sha256(self._ts.digest() + self._val.digest()).hexdigest()


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


def delete_part(store: ObjectStore, tagger: BlobTagger, path: str) -> None:
    """Delete a part and its tag sidecar (a recreated ``part-`` name must not inherit old tags)."""
    store.delete(path)
    tagger.discard(path)


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
    """Rows of ``rows`` (a hot page, SQL order) that must be written; see the module docstring.

    ``cold`` needs the key, ``version``, the value columns and ``ingested_at_ns``. Per key:

    * hot rows **newer than the whole cold history** of the key (``ingested_at_ns`` above every
      cold one) form a sequence that is compared, row by row, with the cold row of the max version
      (as ``ingest.router`` does): an equal value adds nothing, a different one is written with a
      version above everything before it (``RESTATED`` if it had to move). A revert to an earlier
      value is therefore a new version, not a duplicate;
    * all other hot rows keep their version: already cold (same key, version, values,
      ``cold.ingested_at_ns <= hot``) or a re-versioned twin (same key, values and
      ``ingested_at_ns`` at a version ``>=``) are dropped, same key + version with other values
      raises :class:`TieringConflict` (ADR-0014).
    """
    if cold.height == 0:
        return rows
    key, vals = list(spec.key), list(spec.values)
    idx = rows.with_row_index("__i")
    latest = (
        cold.sort([*key, "version", "ingested_at_ns"])
        .group_by(key, maintain_order=True)
        .agg(
            pl.col("version").max().cast(pl.Int64).alias("__cmaxv"),
            pl.col("ingested_at_ns").max().alias("__cmaxi"),
            *[pl.col(v).last().alias(f"{v}__m") for v in vals],
        )
    )
    j = idx.join(latest, on=key, how="left", maintain_order="left")
    is_newer = (pl.col("ingested_at_ns") > pl.col("__cmaxi")).fill_null(False)
    kept = _newer_rows(j.filter(is_newer), key, vals).select(idx.columns)
    return (
        pl.concat([_older_rows(j.filter(~is_newer), cold, spec, idx.columns), kept])
        .sort([*key, "version", "ingested_at_ns"])
        .drop("__i")
    )


def _newer_rows(nw: pl.DataFrame, key: list[str], vals: list[str]) -> pl.DataFrame:
    """The sequence rule of :func:`plan_write` for rows newer than their key's cold history."""
    nw = nw.sort([*key, "version", "ingested_at_ns"]).with_columns(
        pl.int_range(pl.len()).over(key).alias("__rn")
    )
    first = pl.col("__rn") == 0
    same_as_prev = pl.all_horizontal(
        [
            pl.col(v).eq_missing(
                pl.when(first).then(pl.col(f"{v}__m")).otherwise(pl.col(v).shift(1).over(key))
            )
            for v in vals
        ]
    )
    nw = nw.filter(~same_as_prev).with_columns(pl.int_range(pl.len()).over(key).alias("__k"))
    new_version = pl.col("__k") + pl.max_horizontal(
        pl.col("__cmaxv") + 1,
        (pl.col("version").cast(pl.Int64) - pl.col("__k")).cum_max().over(key),
    )
    nw = nw.with_columns(new_version.alias("__nv"))
    moved = pl.col("__nv") != pl.col("version")
    return nw.with_columns(
        pl.col("__nv").cast(pl.Int16).alias("version"),
        pl.when(moved)
        .then(pl.col("quality") | int(QualityFlag.RESTATED))
        .otherwise(pl.col("quality"))
        .cast(pl.Int8)
        .alias("quality"),
    )


def _older_rows(
    old: pl.DataFrame, cold: pl.DataFrame, spec: TableSpec, columns: list[str]
) -> pl.DataFrame:
    """Rows not newer than their cold history: dedupe by version, twins, or conflict."""
    key, vals = list(spec.key), list(spec.values)
    on = [*key, "version"]
    old = old.select(columns)
    have = (
        cold.group_by(on)
        .agg([pl.col(v).last().alias(f"{v}__c") for v in vals])
        .join(cold.group_by(on).agg(pl.col("ingested_at_ns").min().alias("__cing")), on=on)
    )
    j = old.join(have, on=on, how="left", maintain_order="left")
    present = pl.col("__cing").is_not_null()
    same = pl.all_horizontal([pl.col(v).eq_missing(pl.col(f"{v}__c")) for v in vals])
    already = present & same & (pl.col("__cing") <= pl.col("ingested_at_ns"))
    pending = j.filter(~already.fill_null(False))
    # a re-versioned copy of the row may already be cold: same key, values and ingested_at_ns
    twin = cold.select([*key, "version", "ingested_at_ns", *vals]).rename(
        {"version": "__vc", **{v: f"{v}__t" for v in vals}}
    )
    twin_hit = (
        pending.select(["__i", *key, "version", "ingested_at_ns", *vals])
        .join(twin, on=[*key, "ingested_at_ns"], how="inner")
        .filter(
            (pl.col("__vc") >= pl.col("version"))
            & pl.all_horizontal([pl.col(v).eq_missing(pl.col(f"{v}__t")) for v in vals])
        )["__i"]
    )
    pending = pending.filter(~pl.col("__i").is_in(twin_hit.implode()))
    if pending.filter(present & ~same).height:
        raise TieringConflict("same key and version as a cold row with other values, not newer")
    return pending.select(columns)


def _load_manifest(
    store: ObjectStore, mpath: str
) -> tuple[bytes | None, dict[str, dict[str, Any]]]:
    """Raw manifest bytes and its entries by part name (``(None, {})`` if absent or unreadable)."""
    try:
        raw = store.read(mpath)
        return raw, {e["name"]: e for e in json.loads(raw).get("parts", [])}
    except (NotFound, ValueError, KeyError, TypeError):
        return None, {}


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


def listed_sizes(store: ObjectStore, prefix: str) -> dict[str, int] | None:
    """Byte size of every blob under ``prefix`` as the store lists it (``None``: store unknown)."""
    if isinstance(store, LocalStore):
        return {p: store.root.joinpath(p).stat().st_size for p in store.list(prefix)}
    if isinstance(store, AzureBlobStore):
        container, name = layout.split_container(prefix)
        client = store.service.get_container_client(container)
        return {f"{container}/{b.name}": int(b.size) for b in client.list_blobs(name)}
    return None


def _fresh(entry: dict[str, Any] | None, path: str, sizes: dict[str, int] | None) -> bool:
    """A manifest entry is only trusted if its recorded size is the listed size of the part."""
    return entry is not None and (sizes is None or sizes.get(path) == entry.get("bytes"))


def overlapping_parts(  # noqa: PLR0917
    store: ObjectStore, dataset: str, iid: str, year: int, month: int, window: tuple[int, int]
) -> list[str]:
    """Parts whose manifest min/max overlaps ``[lo, hi)``.

    A manifest entry whose ``bytes`` differ from the listed size (stale after a replaced part) is
    ignored and the footer is read instead, as for parts the manifest does not list.
    """
    paths = part_paths(store, dataset, iid, year, month)
    if not paths:
        return []
    _, known = _load_manifest(store, layout.md_manifest_path(dataset, iid, year, month))
    sizes = listed_sizes(store, layout.md_partition_prefix(dataset, iid, year, month))
    out = []
    for path in paths:
        e = known.get(path.rsplit("/", 1)[1])
        if not _fresh(e, path, sizes):
            e = _entry(path, store.read(path))
        assert e is not None  # noqa: S101 - narrowing for mypy
        lo, hi = e["min_ts_ns"], e["max_ts_ns"]
        if lo is None or (lo < window[1] and hi >= window[0]):
            out.append(path)
    return out


def update_manifest(  # noqa: PLR0917
    store: ObjectStore,
    dataset: str,
    iid: str,
    year: int,
    month: int,
    known: Mapping[str, bytes] | None = None,
    paths: Sequence[str] | None = None,
) -> None:
    """Rebuild the partition's ``_manifest.json`` from its parts (derived, deterministic).

    Entries of unchanged parts are reused from the old manifest; ``known`` supplies bytes just
    written so they are not downloaded again. The file is replaced (delete + create-only put); a
    crash in between loses only this derived file, and the next run rebuilds it.
    """
    mpath = layout.md_manifest_path(dataset, iid, year, month)
    old_bytes, old = _load_manifest(store, mpath)
    known = known or {}
    sizes = listed_sizes(store, layout.md_partition_prefix(dataset, iid, year, month))
    entries: list[dict[str, Any]] = []  # parsed JSON
    for path in paths if paths is not None else part_paths(store, dataset, iid, year, month):
        name = path.rsplit("/", 1)[1]
        if path not in known and _fresh(old.get(name), path, sizes):
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


def tag_partition(  # noqa: PLR0917
    store: ObjectStore,
    tagger: BlobTagger,
    now_year: int,
    dataset: str,
    iid: str,
    year: int,
    month: int,
    paths: Sequence[str] | None = None,
) -> int:
    """Ensure the tags of every part of a partition; returns the number of tag writes."""
    todo = paths if paths is not None else part_paths(store, dataset, iid, year, month)
    return sum(ensure_tags(tagger, p, now_year) for p in todo)


# -- hot side ------------------------------------------------------------------------------------
def _fetch(conn: Connection, query: Any, spec: TableSpec) -> pl.DataFrame:
    """Run ``query`` in bounded fetches and return one normalised frame."""
    schema = {
        c: pl.Float64() if c in {"open", "high", "low", "close", "volume", "price", "size"}
        else pl.Int64()
        for c in spec.columns
    }  # fmt: skip
    frames = [
        pl.DataFrame([tuple(r) for r in chunk], schema=schema, orient="row")
        for chunk in conn.execution_options(stream_results=True)
        .execute(query)
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


def oldest_ts(  # noqa: PLR0917
    engine: Engine, spec: TableSpec, key: int, code: int, target: int, floor: int | None = None
) -> int | None:
    """Oldest ``ts_ns`` of a series in ``[floor, target)`` (``None`` if it has none)."""
    t = spec.table
    q = select(func.min(t.c.ts_ns)).where(*_series_conds(spec, key, code), t.c.ts_ns < target)
    if floor is not None:
        q = q.where(t.c.ts_ns >= floor)
    with engine.connect() as conn:
        lo = conn.execute(q).scalar()
    return None if lo is None else int(lo)


def pages(  # noqa: PLR0917
    engine: Engine, spec: TableSpec, key: int, code: int, window: tuple[int, int], page_rows: int
) -> Iterator[pl.DataFrame]:
    """Rows of ``[lo, hi)`` in SQL order, ``page_rows`` at a time, never splitting a timestamp."""
    t = spec.table
    conds = _series_conds(spec, key, code)
    order = [t.c[c] for c in (*spec.key, "version")]
    cols = [t.c[c] for c in spec.columns]
    cursor, hi = window
    while cursor < hi:
        q = (
            select(*cols)
            .where(*conds, t.c.ts_ns >= cursor, t.c.ts_ns < hi)
            .order_by(*order)
            .limit(page_rows)
        )
        with engine.connect() as conn:
            df = _fetch(conn, q, spec)
            if df.height == 0:
                return
            last = int(df["ts_ns"][-1])
            if df.height == page_rows:
                if int(df["ts_ns"][0]) == last:  # one timestamp fills the page: read it whole
                    q = select(*cols).where(*conds, t.c.ts_ns == last).order_by(*order)
                    df = _fetch(conn, q, spec)
                else:
                    df = df.filter(pl.col("ts_ns") < last)
                    last = int(df["ts_ns"][-1])
        yield df
        cursor = last + 1


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


@dataclass(frozen=True, slots=True)
class DeleteChunk:
    """One SQL delete: the series, the inclusive ts range and how many rows were read in it."""

    key: int
    code: int
    lo: int
    hi: int
    rows: int


def delete_chunk(engine: Engine, spec: TableSpec, chunk: DeleteChunk) -> int:
    """Delete exactly the rows read for ``chunk`` in one short transaction.

    Raises :class:`TieringDeleteMismatch` (and rolls back) if the range holds more rows than were
    read, e.g. a restatement inserted meanwhile.
    """
    t = spec.table
    with engine.begin() as conn:
        res = conn.execute(
            delete(t).where(
                *_series_conds(spec, chunk.key, chunk.code),
                t.c.ts_ns >= chunk.lo,
                t.c.ts_ns <= chunk.hi,
            )
        )
        if res.rowcount > chunk.rows:  # rollback on exit: those rows were not verified as cold
            raise TieringDeleteMismatch("delete chunk matched rows that were not migrated")
        return int(res.rowcount)


# -- the job -------------------------------------------------------------------------------------
@dataclass(slots=True)
class _MonthStats:
    chunks: list[DeleteChunk] = field(default_factory=list)
    total: int = 0
    moved: int = 0
    swept: int = 0
    checksum: RunningChecksum = field(default_factory=RunningChecksum)


@dataclass(slots=True)
class _Run:
    """Collaborators and counters shared by the steps of one table run."""

    engine: Engine
    store: ObjectStore
    tagger: BlobTagger
    cfg: TieringConfig
    spec: TableSpec
    lock: AppLock
    now_year: int
    wm: int | None
    res: TableResult
    stats: _MonthStats = field(default_factory=_MonthStats)


def _plan_month(
    r: _Run,
    series: tuple[int, int, str, Interval | None],
    window: tuple[int, int],
    cold: pl.DataFrame,
) -> Iterator[pa.Table]:
    """Pages of the month -> planned contract tables; fills the stats on the way."""
    key, code, iid, interval = series
    st = r.stats
    for page in pages(r.engine, r.spec, key, code, window, r.cfg.batch_rows):
        r.lock.check()
        st.total += page.height
        ts = page["ts_ns"].to_numpy()
        if r.wm is not None:
            st.swept += int((ts < r.wm).sum())
        st.chunks.extend(
            DeleteChunk(key, code, int(ts[a]), int(ts[b - 1]), b - a)
            for a, b in chunk_bounds(ts, r.cfg.delete_chunk_rows)
        )
        todo = plan_write(page, cold, r.spec)
        if todo.height:
            st.moved += todo.height
            st.checksum.update(todo["ts_ns"].to_numpy(), todo[r.spec.checksum_col].to_numpy())
            yield to_contract_table(todo, r.spec, iid, interval)


def _migrate_month(
    r: _Run, series: tuple[int, int, str, Interval | None], window: tuple[int, int]
) -> None:
    _, _, iid, interval = series
    spec, store = r.spec, r.store
    dataset = spec.dataset(interval)
    year, month = partition_of(dataset, window[0])
    cols = [*spec.key, "version", *spec.values, "ingested_at_ns"]
    cold = read_cold(
        store, overlapping_parts(store, dataset, iid, year, month, window), cols, window
    )
    st = r.stats = _MonthStats()
    if r.cfg.dry_run:
        for _ in _plan_month(r, series, window, cold):
            pass
        _account(r, st, deleted=0)
        return
    source = hashlib.sha256(
        f"tiering:{spec.name}:{iid}:{window[0]}:{window[1]}".encode()
    ).hexdigest()
    paths = write_partition(  # 3
        store, dataset=dataset, instrument_id=iid, year=year, month=month,
        batches=_plan_month(r, series, window, cold), source_hash=source,
    )  # fmt: skip
    known: dict[str, bytes] = {}
    got = RunningChecksum()
    for path in paths:  # 4
        data = store.read(path)
        known[path] = data
        tbl = pq.read_table(io.BytesIO(data), columns=["ts_ns", spec.checksum_col])
        got.update(tbl.column("ts_ns").to_numpy(), tbl.column(spec.checksum_col).to_numpy())
    if got.rows != st.moved or got.hexdigest() != st.checksum.hexdigest():
        for path in paths:
            delete_part(store, r.tagger, path)
        raise TieringVerifyFailed(f"written parts for {dataset}/{iid} differ from the SQL rows")
    everything = part_paths(store, dataset, iid, year, month)
    tag_partition(store, r.tagger, r.now_year, dataset, iid, year, month, everything)  # 5
    update_manifest(store, dataset, iid, year, month, known, everything)  # 6
    deleted = 0
    for chunk in st.chunks:  # 7
        r.lock.check()
        deleted += delete_chunk(r.engine, spec, chunk)
    r.res.parts_written += len(paths)
    r.res.bytes_written += sum(len(b) for b in known.values())
    _account(r, st, deleted)


def _account(r: _Run, st: _MonthStats, deleted: int) -> None:
    r.res.batches += 1
    r.res.rows_moved += st.moved
    r.res.rows_deduped += st.total - st.moved
    r.res.rows_deleted += deleted
    r.res.rows_swept += st.swept


def _drain_series(r: _Run, series: tuple[int, int, str, Interval | None], target: int) -> None:
    key, code = series[0], series[1]
    prev: int | None = None
    floor: int | None = None  # only a dry run advances it: nothing leaves SQL there
    while (lo := oldest_ts(r.engine, r.spec, key, code, target, floor)) is not None:
        if lo == prev:  # the previous month is still in SQL: never loop forever
            raise TieringDeleteMismatch("no progress: the month was not removed from SQL")
        prev = lo
        moment = from_ts_ns(lo)
        window = (lo, min(month_bounds(moment.year, moment.month)[1], target))
        r.lock.check()
        _migrate_month(r, series, window)
        if r.cfg.dry_run:
            floor, prev = window[1], None


def run_table(  # noqa: PLR0917
    engine: Engine,
    store: ObjectStore,
    tagger: BlobTagger,
    clock: Clock,
    cfg: TieringConfig,
    spec: TableSpec,
) -> TableResult:
    """Migrate one hot table (step order in the module docstring); never raises on data errors.

    Data problems (:class:`TidalError`) stop the affected series, are listed in
    ``TableResult.errors`` and keep the watermark where it is. :class:`LockLost` aborts the run.
    """
    res = TableResult(spec.name, dry_run=cfg.dry_run)
    with applock(engine, f"tiering:{spec.name}") as lock:
        if not lock:
            res.locked = True
            log.info("tiering_skipped_locked", table=spec.name)
            return res
        repo = sr.SqlRepository(engine, clock)
        wm = repo.get_watermark(spec.name)
        moment = from_ts_ns(clock.now_ns() - cfg.hot_window_days * DAY_NS)
        target = month_bounds(moment.year, moment.month)[0]
        res.target_ns, res.watermark_before = target, wm
        res.sql_kb_before = space_used_kb(engine, spec.name)
        ids = {v: k for k, v in repo.instrument_keys().items()}
        run = _Run(engine, store, tagger, cfg, spec, lock, clock.now().year, wm, res)
        for key, code in list_series(engine, spec, target):
            series = (key, code, ids[key], _BY_CODE[code] if spec.bars else None)
            try:
                _drain_series(run, series, target)
            except LockLost:
                raise
            except TidalError as exc:
                res.errors.append(f"{series[2]}: {exc.slug}")
                log.error("tiering_series_failed", table=spec.name, instrument=series[2],
                          error=exc.slug)  # fmt: skip
        if cfg.dry_run or res.errors:
            res.watermark_after = wm
        else:
            lock.check()
            res.watermark_after = target if wm is None else max(wm, target)
            repo.set_watermark(spec.name, res.watermark_after)  # 8
        res.sql_kb_after = space_used_kb(engine, spec.name)
    log.info("tiering_table_done", table=spec.name, rows_moved=res.rows_moved,
             rows_deduped=res.rows_deduped, rows_swept=res.rows_swept,
             rows_deleted=res.rows_deleted, bytes_written=res.bytes_written,
             parts_written=res.parts_written, sql_kb_before=res.sql_kb_before,
             sql_kb_after=res.sql_kb_after, watermark=res.watermark_after,
             dry_run=cfg.dry_run, errors=len(res.errors))  # fmt: skip
    return res


def run_tiering(  # noqa: PLR0917
    engine: Engine,
    store: ObjectStore,
    tagger: BlobTagger,
    clock: Clock,
    cfg: TieringConfig,
    tables: Sequence[str] = ("bar_hot", "tick_hot"),
) -> list[TableResult]:
    """Run the migration for ``tables`` (``bar_hot`` or ``md.bar_hot`` style) and return results."""
    return [run_table(engine, store, tagger, clock, cfg, spec_for_table(t)) for t in tables]


def run_job(
    engine: Engine, store: ObjectStore, tagger: BlobTagger, clock: Clock, job: TieringJob
) -> TableResult:
    """Run one ``tiering.job/v1`` message (the Service Bus consumer itself is future work)."""
    cfg = TieringConfig(job.hot_window_days, job.batch_rows, dry_run=job.dry_run)
    return run_tiering(engine, store, tagger, clock, cfg, (job.table,))[0]
