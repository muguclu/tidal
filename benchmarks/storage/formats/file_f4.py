"""F4: naive Parquet, pyarrow defaults (SPEC-02 section 2).

What a data scientist gets from ``pq.write_table(table, path)``: snappy, library-default row groups
and encodings (dictionary where it pays, plain otherwise), a ``timestamp`` column instead of the
int64 ``ts_ns``, no sort order metadata, no per-column tuning.

Deviation from SPEC-02: the timestamp is ``timestamp[ns]`` (pyarrow's own default), not ``[us]``.
The synthetic ticks carry nanoseconds, ``[us]`` would truncate them and break W4's exact
``ts_ns``; the ns type costs F4 nothing extra, so the comparison stays conservative for F5.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Final

import pyarrow as pa
import pyarrow.parquet as pq

from benchmarks.storage.formats.file_base import FileFormat, put_file
from tidal.storage import layout
from tidal.storage.parquet_writer import next_seq

_FIELDS: Final["list[pa.Field[Any]]"] = [
    pa.field("ts", pa.timestamp("ns")),
    pa.field("instrument_id", pa.string()),
    pa.field("price", pa.float64()),
    pa.field("size", pa.float32()),
    pa.field("side", pa.int8()),
    pa.field("quality", pa.int8()),
    pa.field("ingested_at_ns", pa.int64()),
    pa.field("version", pa.int16()),
]
NAIVE_SCHEMA = pa.schema(_FIELDS)


def to_naive(table: pa.Table) -> pa.Table:
    """``TICK_V1`` -> the naive column layout (timestamp instead of ``ts_ns``)."""
    return pa.table(
        {
            "ts": table.column("ts_ns").cast(pa.timestamp("ns")),
            "instrument_id": table.column("instrument_id").cast(pa.string()),
            **{c: table.column(c) for c in NAIVE_SCHEMA.names[2:]},
        },
        schema=NAIVE_SCHEMA,
    )


class F4Format(FileFormat):
    """Parquet written with library defaults."""

    id = "F4"
    reader: ClassVar[str] = "read_parquet(?)"
    ts_col: ClassVar[str] = "ts"
    ns_expr: ClassVar[str] = "epoch_ns(ts)"
    ts_bound: ClassVar[str] = "make_timestamp_ns(?)"

    def _write_partition(
        self,
        tables: Sequence[pa.Table],
        inst: str,
        y: int,
        m: int,
        source_hash: str,  # noqa: ARG002
    ) -> int:
        """One default-settings Parquet part holding the whole instrument-month."""
        table = to_naive(pa.concat_tables(tables))
        seq = next_seq(self.store, layout.md_partition_prefix("ticks", inst, y, m))
        path = layout.md_part_path("ticks", inst, y, m, seq)

        def write(tmp: Path) -> None:
            pq.write_table(table, tmp, compression="snappy")

        put_file(self.store, path, write)
        self._record_row_groups([path])
        return table.num_rows
