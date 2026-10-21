"""Epoch Parquet writer (storage-layout §3): immutable parts, streamed per partition.

Batches are buffered to whole row groups, written to a local temp file and then handed to an
injected :class:`ObjectStore` (local FS for tests/dev, Azure Blob in the cloud), so peak memory
is about one row group regardless of the partition size.
"""

# ruff: noqa: N818

import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tidal import __version__
from tidal.common.errors import TidalError
from tidal.contracts.schemas import SCHEMAS, pyarrow_write_kwargs
from tidal.storage import layout

if TYPE_CHECKING:
    from azure.storage.blob import BlobServiceClient

COMPRESSION: Final = "zstd"
COMPRESSION_LEVEL: Final = 6
ROW_GROUP_ROWS: Final = 1_000_000
DATA_PAGE_BYTES: Final = 1 << 20
MAX_PART_ROWS: Final = 10_000_000
WRITER_VERSION: Final = f"tidal-{__version__}/pyarrow-{pa.__version__}"


class SchemaMismatch(TidalError):
    """The table does not match the contract schema of its dataset."""

    status = 400
    slug = "schema-mismatch"
    title = "Schema mismatch"


class UnsortedInput(TidalError):
    """``ts_ns`` is not ascending, or lies outside the target partition."""

    status = 400
    slug = "unsorted-input"
    title = "Input not sorted or outside partition"


class PartitionMismatch(TidalError):
    """A batch holds an ``instrument_id`` or ``interval`` other than the target partition's."""

    status = 400
    slug = "partition-mismatch"
    title = "Batch does not belong to the partition"


class ImmutableFileExists(TidalError):
    """A part already exists; files are never overwritten (storage-layout §2)."""

    status = 409
    slug = "immutable-file-exists"
    title = "Part already exists"


class ObjectStore(Protocol):
    """Minimal blob store: container-qualified paths, create-only writes."""

    def list_paths(self, prefix: str) -> list[str]:
        """Return all existing paths starting with ``prefix``."""
        ...

    def put_new(self, path: str, local_file: Path) -> None:
        """Upload ``local_file``; raise :class:`ImmutableFileExists` if ``path`` exists."""
        ...


class LocalStore:
    """Local filesystem store rooted at e.g. ``./.data``, mirroring the blob layout."""

    def __init__(self, root: Path) -> None:
        """Create a store under ``root``."""
        self.root = root

    def list_paths(self, prefix: str) -> list[str]:
        """List existing files below the directory prefix (ending in ``/``)."""
        base = self.root / prefix
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())

    def put_new(self, path: str, local_file: Path) -> None:
        """Copy then ``os.link``: atomic, and fails if the destination already exists."""
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(local_file, tmp)
        try:
            os.link(tmp, dest)
        except FileExistsError:
            raise ImmutableFileExists(f"{path} already exists") from None
        finally:
            tmp.unlink(missing_ok=True)


class AzureBlobStore:
    """Azure Blob store over an injected ``BlobServiceClient`` (Azurite or a real account)."""

    def __init__(self, service: "BlobServiceClient") -> None:
        """Wrap ``service``; credentials are the caller's concern."""
        self.service = service

    def list_paths(self, prefix: str) -> list[str]:
        """List blobs whose name starts with the prefix's blob part."""
        container, name = layout.split_container(prefix)
        client = self.service.get_container_client(container)
        return [f"{container}/{b.name}" for b in client.list_blobs(name_starts_with=name)]

    def put_new(self, path: str, local_file: Path) -> None:
        """Upload with ``overwrite=False``."""
        from azure.core.exceptions import ResourceExistsError  # noqa: PLC0415

        container, name = layout.split_container(path)
        blob = self.service.get_blob_client(container, name)
        try:
            with local_file.open("rb") as fh:
                blob.upload_blob(fh, overwrite=False)
        except ResourceExistsError:
            raise ImmutableFileExists(f"{path} already exists") from None


def schema_name_for(dataset: str) -> str:
    """Contract schema name of a dataset (``ticks`` -> TICK_V1, bars -> BAR_V1)."""
    if dataset not in layout.DATASETS:
        raise layout.InvalidPath(f"unknown dataset {dataset!r}")
    return "TICK_V1" if dataset == "ticks" else "BAR_V1"


def next_seq(store: ObjectStore, prefix: str) -> int:
    """Next free ``part-`` number in a partition (0 if it is empty)."""
    seqs = []
    for p in store.list_paths(prefix):
        try:
            part = layout.parse_md_path(p)
        except layout.InvalidPath:
            continue  # temp files, foreign blobs
        if part.seq is not None:
            seqs.append(part.seq)
    return max(seqs) + 1 if seqs else 0


def partition_bounds_ns(year: int, month: int) -> tuple[int, int]:
    """Half-open ``[start, end)`` epoch-ns of a partition (month 0 = the whole year)."""
    if month == 0:
        start = np.datetime64(f"{year:04d}-01", "M")
        end = start + np.timedelta64(12, "M")
    else:
        start = np.datetime64(f"{year:04d}-{month:02d}", "M")
        end = start + np.timedelta64(1, "M")
    return (
        int(start.astype("datetime64[ns]").astype(np.int64)),
        int(end.astype("datetime64[ns]").astype(np.int64)),
    )


@dataclass
class _Sink:
    """Writes one partition as one or more parts, rolling above ``max_rows``."""

    store: ObjectStore
    dataset: str
    instrument_id: str
    year: int
    month: int
    source_hash: str
    max_rows: int
    row_group: int
    written: list[str] = field(default_factory=list)
    _tmp: tempfile.TemporaryDirectory[str] | None = None
    _writer: pq.ParquetWriter | None = None
    _rows: int = 0

    def _open(self) -> None:
        name = schema_name_for(self.dataset)
        prefix = layout.md_partition_prefix(self.dataset, self.instrument_id, self.year, self.month)
        seq = next_seq(self.store, prefix)
        if (
            self.written
        ):  # our own previous part may not be listed by an eventually-consistent store
            seq = max(seq, (layout.parse_md_path(self.written[-1]).seq or 0) + 1)
        path = layout.md_part_path(self.dataset, self.instrument_id, self.year, self.month, seq)
        meta = {
            "tidal.schema": name,
            "tidal.writer_version": WRITER_VERSION,
            "tidal.source_hash": self.source_hash,
        }
        self._tmp = tempfile.TemporaryDirectory(prefix="tidal-part-")
        self._writer = pq.ParquetWriter(
            Path(self._tmp.name) / "part.parquet",
            SCHEMAS[name].with_metadata(meta),
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
            data_page_size=DATA_PAGE_BYTES,
            write_statistics=True,
            sorting_columns=[pq.SortingColumn(0)],  # ts_ns is column 0 in both schemas
            **pyarrow_write_kwargs(name),  # type: ignore[arg-type]  # dict[str, object] by design
        )
        self.written.append(path)
        self._rows = 0

    def write(self, table: pa.Table) -> None:
        """Write ``table``, slicing it so that no part ever exceeds ``max_rows``."""
        offset = 0
        while offset < table.num_rows:
            if self._writer is not None and self._rows >= self.max_rows:
                self.finish()
            if self._writer is None:
                self._open()
            assert self._writer is not None  # noqa: S101 - narrowing for mypy
            chunk = table.slice(offset, min(table.num_rows - offset, self.max_rows - self._rows))
            self._writer.write_table(chunk, row_group_size=self.row_group)
            self._rows += chunk.num_rows
            offset += chunk.num_rows

    def finish(self) -> None:
        """Close the open part and upload it."""
        if self._writer is None or self._tmp is None:
            return
        self._writer.close()
        try:
            self.store.put_new(self.written[-1], Path(self._tmp.name) / "part.parquet")
        finally:
            self._tmp.cleanup()
            self._writer, self._tmp = None, None

    def abort(self) -> None:
        """Drop the open part without uploading; earlier rolled parts stay (immutable)."""
        if self._writer is not None and self._tmp is not None:
            self._writer.close()
            self._tmp.cleanup()
            self._writer, self._tmp = None, None
            self.written.pop()


def _as_table(batch: pa.Table | pa.RecordBatch, name: str) -> pa.Table:
    table = pa.Table.from_batches([batch]) if isinstance(batch, pa.RecordBatch) else batch
    if not table.schema.equals(SCHEMAS[name], check_metadata=False):
        raise SchemaMismatch(f"table does not match {name}")
    return table


def _check_identity(table: pa.Table, dataset: str, instrument_id: str) -> None:
    """Every row must carry the partition's instrument (and interval, for bars)."""
    if set(table.column("instrument_id").unique().to_pylist()) != {instrument_id}:
        raise PartitionMismatch(f"batch contains instruments other than {instrument_id}")
    if dataset != "ticks":
        want = dataset.removeprefix("bars_")
        if set(table.column("interval").unique().to_pylist()) != {want}:
            raise PartitionMismatch(f"batch contains intervals other than {want}")


def write_partition(
    store: ObjectStore,
    *,
    dataset: str,
    instrument_id: str,
    year: int,
    month: int,
    batches: Iterable[pa.Table | pa.RecordBatch],
    source_hash: str,
    row_group_size: int = ROW_GROUP_ROWS,
    max_part_rows: int = MAX_PART_ROWS,
) -> list[str]:
    """Stream ``batches`` (contract schema, ``ts_ns`` ascending) into new immutable parts.

    Returns the written paths (empty if there were no rows). A new ``part-`` is started when the
    current one would exceed ``max_part_rows``. Raises :class:`SchemaMismatch` or
    :class:`UnsortedInput` or :class:`PartitionMismatch` (wrong instrument/interval); a partially
    written part is then discarded, never uploaded.
    """
    name = schema_name_for(dataset)
    lo, hi = partition_bounds_ns(year, month)
    sink = _Sink(
        store, dataset, instrument_id, year, month, source_hash, max_part_rows, row_group_size
    )
    layout.md_partition_prefix(dataset, instrument_id, year, month)  # validates coordinates
    pending: list[pa.Table] = []
    pending_rows = 0
    last_ts = lo
    try:
        for batch in batches:
            table = _as_table(batch, name)
            if table.num_rows == 0:
                continue
            _check_identity(table, dataset, instrument_id)
            ts = table.column("ts_ns").to_numpy()
            if ts[0] < last_ts or ts[-1] >= hi or bool(np.any(ts[1:] < ts[:-1])):
                raise UnsortedInput("ts_ns must ascend and stay inside the partition")
            last_ts = int(ts[-1])
            pending.append(table)
            pending_rows += table.num_rows
            if pending_rows >= row_group_size:  # emit whole row groups only
                whole = pa.concat_tables(pending)
                n = pending_rows // row_group_size * row_group_size
                sink.write(whole.slice(0, n))
                pending, pending_rows = [whole.slice(n)], whole.num_rows - n
        if pending_rows:
            sink.write(pa.concat_tables(pending))
        sink.finish()
    except BaseException:
        sink.abort()
        raise
    return sink.written
