"""Partition compaction (SPEC-03 §3.1, storage-layout §2): merge the parts of a partition.

All versions are kept (data-contracts §7); only exact duplicates (same key *and* version, e.g. the
same bar written to cold twice) collapse, the later part winning. Rows are sorted by
``ts_ns, [side,] version``.

Step order and crash safety, per partition, under ``sp_getapplock('tiering:<table>')`` (the same
lock as the migration, so no migration appends while parts are merged):

1. LIST    snapshot of the parts ``P`` and their rows; ``M`` = merged, de-duplicated rows.
2. KEEP    a part of ``P`` whose own de-duplicated rows equal ``M`` (checksum of all columns) is
           the target; the highest ``seq`` wins. If none exists, WRITE ``M`` as a new ``part-``
           and VERIFY it (count, ``ts_ns``/``close|price`` checksum, full-column digest).
3. TAG     the target ``compacted=true`` (plus the standard tags).
4. DELETE  every other part of the snapshot, then rebuild ``_manifest.json``.

A crash after 2 leaves duplicated rows across parts, which readers de-duplicate; the re-run
recomputes ``M`` from all parts (the new part adds nothing), finds the new part equal to ``M``
and resumes at 3. A crash inside 4 leaves a subset of the old parts; same resume. Parts appended
after the snapshot are never deleted. Nothing is deleted before the target is verified.
"""

# ruff: noqa: PLR0917

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

import polars as pl
import pyarrow.parquet as pq
from sqlalchemy import Engine

from tidal.common.errors import TidalError
from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock
from tidal.contracts.schemas import Interval
from tidal.storage import layout
from tidal.storage.parquet_writer import ObjectStore, write_partition
from tidal.storage.retag import BlobTagger, ensure_tags
from tidal.storage.tiering import (
    TableSpec,
    TieringVerifyFailed,
    applock,
    checksum,
    frame_checksum,
    frame_digest,
    normalise,
    part_paths,
    spec_for_dataset,
    tag_partition,
    to_contract_table,
    update_manifest,
)

log = get_logger(__name__)

_HASH_PREFIX: Final = "compact:"


@dataclass(slots=True)
class CompactResult:
    """Counts of one compaction run."""

    dataset: str
    locked: bool = False
    partitions: int = 0
    merged: int = 0  # partitions that had more than one part
    parts_written: int = 0
    parts_removed: int = 0
    rows: int = 0
    errors: list[str] = field(default_factory=list)


def _merge(frames: Sequence[pl.DataFrame], spec: TableSpec) -> pl.DataFrame:
    """De-duplicate on key + version (later frame wins) and sort by key, version."""
    on = [*spec.key, "version"]
    both = pl.concat(
        [f.with_columns(pl.lit(i, pl.Int64).alias("_seq")) for i, f in enumerate(frames)]
    )
    return (
        both.sort("_seq", maintain_order=True)
        .unique(subset=on, keep="last", maintain_order=True)
        .sort(on)
        .drop("_seq")
    )


def _read_part(store: ObjectStore, path: str) -> pl.DataFrame:
    """All columns of one part except the constant identity columns."""
    table = pq.read_table(io.BytesIO(store.read(path)))
    table = table.drop_columns(
        [c for c in ("instrument_id", "interval") if c in table.column_names]
    )
    frame = pl.from_arrow(table)
    assert isinstance(frame, pl.DataFrame)  # noqa: S101 - narrowing for mypy
    return normalise(frame)


def compact_partition(
    store: ObjectStore,
    tagger: BlobTagger,
    now_year: int,
    dataset: str,
    iid: str,
    year: int,
    month: int,
) -> tuple[int, int, int]:
    """Compact one partition; returns ``(parts_written, parts_removed, rows)``."""
    spec = spec_for_dataset(dataset)
    interval = None if dataset == "ticks" else Interval(dataset.removeprefix("bars_"))
    paths = part_paths(store, dataset, iid, year, month)  # 1
    if not paths:
        return 0, 0, 0
    frames = [_read_part(store, p) for p in paths]
    merged = _merge(frames, spec)
    want = frame_digest(merged)
    keep: str | None = None
    for path, frame in zip(paths, frames, strict=True):
        if frame.height == merged.height and frame_digest(_merge([frame], spec)) == want:
            keep = path  # 2: the highest equal part wins
    written = 0
    known: dict[str, bytes] = {}
    if keep is None:
        (keep,) = write_partition(
            store, dataset=dataset, instrument_id=iid, year=year, month=month,
            batches=[to_contract_table(merged, spec, iid, interval)],
            source_hash=_HASH_PREFIX + want,
        )  # fmt: skip
        known[keep] = store.read(keep)
        tbl = pq.read_table(io.BytesIO(known[keep]), columns=["ts_ns", spec.checksum_col])
        if tbl.num_rows != merged.height or checksum(
            tbl.column("ts_ns").to_numpy(), tbl.column(spec.checksum_col).to_numpy()
        ) != frame_checksum(merged, spec):
            raise TieringVerifyFailed(f"compacted part of {dataset}/{iid} differs from its inputs")
        if frame_digest(_read_part(store, keep)) != want:
            raise TieringVerifyFailed(f"compacted part of {dataset}/{iid} has other columns")
        written = 1
    ensure_tags(tagger, keep, now_year, compacted=True)  # 3
    removed = 0
    for path in paths:  # 4
        if path != keep:
            store.delete(path)
            removed += 1
    tag_partition(store, tagger, now_year, dataset, iid, year, month)  # later parts keep theirs
    update_manifest(store, dataset, iid, year, month, known)
    return written, removed, merged.height


def compact(
    engine: Engine,
    store: ObjectStore,
    tagger: BlobTagger,
    clock: Clock,
    dataset: str,
    *,
    instrument_id: str | None = None,
    year: int | None = None,
) -> CompactResult:
    """Compact every partition of ``dataset`` (optionally one instrument and/or year).

    A failing partition is recorded in ``errors`` and skipped; the others still run.
    """
    spec = spec_for_dataset(dataset)
    res = CompactResult(dataset)
    seen: set[tuple[str, int, int]] = set()
    with applock(engine, f"tiering:{spec.name}") as got:
        if not got:
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
            try:
                w, r, rows = compact_partition(store, tagger, now_year, dataset, iid, y, m)
            except TidalError as exc:
                res.errors.append(f"{iid}/{y}/{m:02d}: {exc.slug}")
                log.error("compact_failed", dataset=dataset, instrument=iid, year=y, error=exc.slug)
                continue
            res.partitions += 1
            res.merged += int(r > 0)
            res.parts_written += w
            res.parts_removed += r
            res.rows += rows
    log.info("compact_done", dataset=dataset, partitions=res.partitions, merged=res.merged,
             parts_written=res.parts_written, parts_removed=res.parts_removed,
             errors=len(res.errors))  # fmt: skip
    return res
