"""F3: gzip CSV on Blob, the common "data lake" anti-pattern (SPEC-02 section 2).

One ``part-NNNNN.csv.gz`` per instrument-month. Columns mirror ``TICK_V1``; the timestamp is an
ISO-8601 UTC string with nanoseconds (``2024-11-01 01:00:00.203615214``), prices are the shortest
round-trip decimal text (lossless, negative prices kept), gzip level 6. DuckDB parses the text
back into ``TIMESTAMP_NS``; nothing can be skipped, every query decompresses and parses whole files.
"""

import gzip
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import polars as pl
import pyarrow as pa

from benchmarks.storage.formats.file_base import FileFormat, put_file
from tidal.storage import layout

GZIP_LEVEL = 6
_TS_FORMAT = "%Y-%m-%d %H:%M:%S%.9f"
_COLUMNS = (
    "{'ts':'TIMESTAMP_NS','instrument_id':'VARCHAR','price':'DOUBLE','size':'FLOAT',"
    "'side':'TINYINT','quality':'TINYINT','ingested_at_ns':'BIGINT','version':'SMALLINT'}"
)


def to_csv_bytes(table: pa.Table) -> bytes:
    """``TICK_V1`` table -> CSV bytes (header included) as described in the module docstring."""
    df = pl.from_arrow(table)
    assert isinstance(df, pl.DataFrame)
    df = df.select(
        pl.col("ts_ns").cast(pl.Datetime("ns")).alias("ts"),
        pl.col("instrument_id").cast(pl.Utf8),
        "price",
        "size",
        "side",
        "quality",
        "ingested_at_ns",
        "version",
    )
    return df.write_csv(datetime_format=_TS_FORMAT).encode()


class F3Format(FileFormat):
    """gzip CSV, row-oriented text."""

    id = "F3"
    reader: ClassVar[str] = (
        f"read_csv(?, header = true, columns = {_COLUMNS}, compression = 'gzip')"
    )
    ts_col: ClassVar[str] = "ts"
    ns_expr: ClassVar[str] = "epoch_ns(ts)"
    ts_bound: ClassVar[str] = "make_timestamp_ns(?)"
    compressed_input: ClassVar[bool] = True

    def _write_partition(
        self,
        tables: Sequence[pa.Table],
        inst: str,
        y: int,
        m: int,
        source_hash: str,  # noqa: ARG002
    ) -> int:
        """One gzip CSV part holding the whole instrument-month."""
        table = pa.concat_tables(tables)
        seq = sum(1 for _ in self.store.list(layout.md_partition_prefix("ticks", inst, y, m)))
        path = layout.md_part_path("ticks", inst, y, m, seq).removesuffix(".parquet") + ".csv.gz"

        def write(tmp: Path) -> None:
            # mtime=0 and no file name: identical input gives identical bytes (determinism)
            with (
                tmp.open("wb") as raw,
                gzip.GzipFile(
                    filename="", mode="wb", compresslevel=GZIP_LEVEL, fileobj=raw, mtime=0
                ) as fh,
            ):
                fh.write(to_csv_bytes(table))

        put_file(self.store, path, write)
        return table.num_rows
