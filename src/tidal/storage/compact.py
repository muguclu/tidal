"""Partition compaction (SPEC-03 §3.1, storage-layout §2): merge the parts of a partition.

All versions are kept (data-contracts §7). Rows with the same key, version **and values** collapse
to one, keeping the **minimum** ``ingested_at_ns`` (an as-of read must still see the earliest
ingestion); the same key + version with different values raises :class:`TieringConflict` and the
partition is left untouched. Rows are sorted by ``ts_ns, [side,] version``. A merged partition
larger than ``MAX_PART_ROWS`` becomes several consecutive parts.

Step order and crash safety, per partition, under ``sp_getapplock('tiering:<table>')`` (the same
lock as the migration, so no migration appends while parts are merged):

1. LIST    snapshot of the parts ``P`` and their rows; ``M`` = merged rows, ``D`` = digest of ``M``.
2. KEEP    the target is (a) the complete set of output parts of an earlier compaction of the same
           ``D`` (footer ``tidal.source_hash = compact:<D>:<k>/<n>``), else (b) a single part whose
           own rows equal ``M``. If neither exists, WRITE ``M`` as ``n`` new parts and VERIFY them
           (count, ``ts_ns``/``close|price`` checksum, digest of all columns); on a failure the
           new parts are deleted before the error is raised.
3. TAG     the target parts ``compacted=true`` (plus ``age_band``); ticks only (ADR-0015), the
           other datasets are compacted but never tagged.
4. DELETE  every other part of the snapshot (and its tag sidecar), then rebuild the manifest.

A partition that is already a single part is skipped without downloading data: for a tagged
dataset when the part carries ``compacted=true``, for an untagged one when its manifest lists
exactly that one part (nothing to merge).

A crash after 2 leaves duplicated rows across parts, which readers de-duplicate; the re-run
recomputes ``M`` from all parts (the new parts add nothing), finds the complete output set and
resumes at 3. A crash inside 4 leaves a subset of the old parts; same resume. Parts appended after
the snapshot are never deleted. Nothing is deleted before the target is verified.
"""

import io
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import polars as pl
import pyarrow.parquet as pq
from sqlalchemy import Engine

from tidal.common.errors import NotFound, TidalError
from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock
from tidal.contracts.schemas import Interval
from tidal.storage import layout
from tidal.storage.parquet_writer import MAX_PART_ROWS, ObjectStore, write_partition
from tidal.storage.retag import TAG_COMPACTED, BlobTagger, ensure_tags, is_tagged
from tidal.storage.tiering import (
    RunningChecksum,
    TableSpec,
    TieringConflict,
    TieringVerifyFailed,
    applock,
    delete_part,
    frame_digest,
    normalise,
    part_paths,
    spec_for_dataset,
    tag_partition,
    to_contract_table,
    update_manifest,
)

log = get_logger(__name__)

_HASH_PREFIX = "compact:"
SKIPPED = (0, 0, -1)  # compact_partition result of a single already-compacted part


@dataclass(slots=True)
class CompactResult:
    """Counts of one compaction run."""

    dataset: str
    locked: bool = False
    partitions: int = 0
    skipped: int = 0  # single already-compacted parts, not downloaded
    merged: int = 0  # partitions that had more than one part
    parts_written: int = 0
    parts_removed: int = 0
    rows: int = 0
    errors: list[str] = field(default_factory=list)


def merge_frames(frames: Sequence[pl.DataFrame], spec: TableSpec) -> pl.DataFrame:
    """De-duplicate equal rows (keep the minimum ``ingested_at_ns``), sorted by key, version.

    Raises :class:`TieringConflict` if one key + version carries different values.
    """
    on = [*spec.key, "version"]
    both = pl.concat(frames)
    distinct = both.group_by(on).agg(pl.struct(list(spec.values)).n_unique().alias("__n"))
    if (distinct["__n"] > 1).any():
        raise TieringConflict("one key and version has different values in the parts")
    return (
        both.sort([*on, "ingested_at_ns"])
        .unique(subset=on, keep="first", maintain_order=True)
        .sort(on)
    )


def _read_part(store: ObjectStore, path: str) -> tuple[pl.DataFrame, str]:
    """All columns of one part except the constant identity columns, plus its ``source_hash``."""
    table = pq.read_table(io.BytesIO(store.read(path)))
    meta = table.schema.metadata or {}
    table = table.drop_columns(
        [c for c in ("instrument_id", "interval") if c in table.column_names]
    )
    frame = pl.from_arrow(table)
    assert isinstance(frame, pl.DataFrame)  # noqa: S101 - narrowing for mypy
    return normalise(frame), meta.get(b"tidal.source_hash", b"").decode()


def _complete_set(paths: Sequence[str], hashes: Sequence[str], digest: str) -> list[str]:
    """Output parts of an earlier compaction of ``digest`` if all ``n`` of them are present."""
    prefix = f"{_HASH_PREFIX}{digest}:"
    found: dict[int, str] = {}
    total = 0
    for path, h in zip(paths, hashes, strict=True):
        if h.startswith(prefix):
            k, _, n = h.removeprefix(prefix).partition("/")
            found[int(k)], total = path, int(n)
    return [found[k] for k in sorted(found)] if total and len(found) == total else []


def _write_merged(  # noqa: PLR0917
    store: ObjectStore,
    tagger: BlobTagger,
    spec: TableSpec,
    part: tuple[str, str, int, int],
    merged: pl.DataFrame,
    max_part_rows: int,
) -> list[str]:
    """Write ``merged`` as consecutive parts and verify them; roll them back on any failure."""
    dataset, iid, year, month = part
    interval = None if dataset == "ticks" else Interval(dataset.removeprefix("bars_"))
    digest = frame_digest(merged)
    n = max(1, math.ceil(merged.height / max_part_rows))
    written: list[str] = []
    want = RunningChecksum()
    want.update(merged["ts_ns"].to_numpy(), merged[spec.checksum_col].to_numpy())
    try:
        for k in range(n):
            chunk = merged.slice(k * max_part_rows, max_part_rows)
            written += write_partition(
                store, dataset=dataset, instrument_id=iid, year=year, month=month,
                batches=[to_contract_table(chunk, spec, iid, interval)],
                source_hash=f"{_HASH_PREFIX}{digest}:{k}/{n}", max_part_rows=max_part_rows,
            )  # fmt: skip
        got = RunningChecksum()
        back = [_read_part(store, p)[0] for p in written]
        for frame in back:
            got.update(frame["ts_ns"].to_numpy(), frame[spec.checksum_col].to_numpy())
        if got.hexdigest() != want.hexdigest() or frame_digest(pl.concat(back)) != digest:
            raise TieringVerifyFailed(f"compacted parts of {dataset}/{iid} differ from the inputs")
    except BaseException:
        for path in written:  # only the new parts are touched on a failure
            delete_part(store, tagger, path)
        raise
    return written


def _already_compacted(
    store: ObjectStore, tagger: BlobTagger, part: tuple[str, str, int, int], paths: Sequence[str]
) -> bool:
    """A single part that needs no merge; no part data is read.

    Tagged datasets: the part is marked ``compacted=true``. Untagged datasets: the manifest lists
    exactly that one part.
    """
    if len(paths) != 1:
        return False
    dataset, iid, year, month = part
    if is_tagged(dataset):
        try:
            return tagger.get_tags(paths[0]).get(TAG_COMPACTED) == "true"
        except NotFound:
            return False
    try:
        raw = store.read(layout.md_manifest_path(dataset, iid, year, month))
        listed = [e["name"] for e in json.loads(raw)["parts"]]
    except (NotFound, ValueError, KeyError, TypeError):
        return False
    return listed == [paths[0].rsplit("/", 1)[1]]


def compact_partition(
    store: ObjectStore,
    tagger: BlobTagger,
    now_year: int,
    part: tuple[str, str, int, int],
    max_part_rows: int = MAX_PART_ROWS,
) -> tuple[int, int, int]:
    """Compact partition ``(dataset, instrument, year, month)``; returns written, removed, rows.

    ``SKIPPED`` for a single already-compacted part.
    """
    dataset, iid, year, month = part
    spec = spec_for_dataset(dataset)
    paths = part_paths(store, dataset, iid, year, month)  # 1
    if not paths:
        return 0, 0, 0
    if _already_compacted(store, tagger, part, paths):
        return SKIPPED
    read = [_read_part(store, p) for p in paths]
    frames = [f for f, _ in read]
    merged = merge_frames(frames, spec)
    digest = frame_digest(merged)
    keep = _complete_set(paths, [h for _, h in read], digest)  # 2
    if not keep:
        for path, frame in zip(paths, frames, strict=True):
            if (
                frame.height == merged.height
                and frame_digest(merge_frames([frame], spec)) == digest
            ):
                keep = [path]
    written: list[str] = []
    if not keep:
        written = _write_merged(store, tagger, spec, part, merged, max_part_rows)
        keep = written
    if is_tagged(dataset):
        for path in keep:  # 3
            ensure_tags(tagger, path, now_year, compacted=True)
    removed = 0
    for path in paths:  # 4
        if path not in keep:
            delete_part(store, tagger, path)
            removed += 1
    tag_partition(store, tagger, now_year, dataset, iid, year, month)  # later parts keep theirs
    update_manifest(store, dataset, iid, year, month)
    return len(written), removed, merged.height


def compact(
    engine: Engine,
    store: ObjectStore,
    tagger: BlobTagger,
    clock: Clock,
    dataset: str,
    *,
    instrument_id: str | None = None,
    year: int | None = None,
    max_part_rows: int = MAX_PART_ROWS,
) -> CompactResult:
    """Compact every partition of ``dataset`` (optionally one instrument and/or year).

    A failing partition is recorded in ``errors`` and skipped; the others still run.
    """
    spec = spec_for_dataset(dataset)
    res = CompactResult(dataset)
    seen: set[tuple[str, int, int]] = set()
    with applock(engine, f"tiering:{spec.name}") as lock:
        if not lock:
            res.locked = True
            return res
        now_year = clock.now().year
        for path in store.list(f"{layout.CONTAINER_MARKETDATA}/{layout.MD_VERSION}/{dataset}/"):
            try:
                part = layout.parse_md_path(path)
            except layout.InvalidPath:
                continue
            if (instrument_id and part.instrument_id != instrument_id) or (
                year is not None and part.year != year
            ):
                continue
            seen.add((part.instrument_id, part.year, part.month))
        for iid, y, m in sorted(seen):
            lock.check()
            try:
                w, r, rows = compact_partition(
                    store, tagger, now_year, (dataset, iid, y, m), max_part_rows
                )
            except TidalError as exc:
                res.errors.append(f"{iid}/{y}/{m:02d}: {exc.slug}")
                log.error("compact_failed", dataset=dataset, instrument=iid, year=y, error=exc.slug)
                continue
            res.partitions += 1
            if rows < 0:
                res.skipped += 1
                continue
            res.merged += int(r > 0)
            res.parts_written += w
            res.parts_removed += r
            res.rows += rows
    log.info("compact_done", dataset=dataset, partitions=res.partitions, merged=res.merged,
             parts_written=res.parts_written, parts_removed=res.parts_removed,
             errors=len(res.errors))  # fmt: skip
    return res
