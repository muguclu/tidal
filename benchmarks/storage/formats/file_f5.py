"""F5: the Tidal format, epoch-optimised Parquet (storage-layout section 3), and its F6 sweeps.

F5 is written by :func:`tidal.storage.parquet_writer.write_partition` unchanged (sorted ``ts_ns``
int64 with DELTA_BINARY_PACKED, BYTE_STREAM_SPLIT floats, zstd 6, 1M-row groups, statistics and
sort metadata). F6 re-runs it with another zstd level and/or row-group size: the writer keeps the
level in a module constant, so :class:`F5Format` swaps ``COMPRESSION_LEVEL`` around the call
(the one place a setting is overridden; every other setting still comes from the writer).
"""

from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa

from benchmarks.storage.formats.file_base import BlobTarget, FileFormat
from tidal.storage import parquet_writer as pw

F6_LEVELS = (3, 6, 9)
F6_ROW_GROUPS = (128_000, 1_000_000, 4_000_000)
F6_DEFAULT = (9, 1_000_000)  # what the bare "F6" id means (the sweep ids cover the grid)


def rg_label(rows: int) -> str:
    """``128000`` -> ``128k``, ``1000000`` -> ``1m``."""
    return f"{rows // 1_000_000}m" if rows % 1_000_000 == 0 else f"{rows // 1000}k"


class F5Format(FileFormat):
    """Epoch-optimised Parquet read with DuckDB (also the base of the F6 sweep)."""

    id = "F5"

    def __init__(
        self,
        workdir: Path,
        target: BlobTarget | None = None,
        *,
        level: int = pw.COMPRESSION_LEVEL,
        row_group: int = pw.ROW_GROUP_ROWS,
        sweep: bool = False,
    ) -> None:
        """Defaults are the contract values (F5); ``sweep=True`` labels the object an F6 point."""
        self.level, self.row_group = level, row_group
        if sweep:
            self.id = f"F6-z{level}-rg{rg_label(row_group)}"
        super().__init__(workdir, target)

    def _write_partition(
        self, tables: Sequence[pa.Table], inst: str, y: int, m: int, source_hash: str
    ) -> int:
        """Stream the tables through ``write_partition`` (restatements land as the next part)."""
        old = pw.COMPRESSION_LEVEL
        pw.COMPRESSION_LEVEL = self.level  # type: ignore[misc]  # Final in the writer; see module doc
        try:
            pw.write_partition(
                self.store, dataset="ticks", instrument_id=inst, year=y, month=m,
                batches=tables, source_hash=source_hash, row_group_size=self.row_group,
            )  # fmt: skip
        finally:
            pw.COMPRESSION_LEVEL = old  # type: ignore[misc]
        return sum(t.num_rows for t in tables)


def f6_ids() -> list[str]:
    """Ids of the full 3 x 3 sweep, e.g. ``F6-z3-rg128k`` ... ``F6-z9-rg4m``."""
    return [f"F6-z{lv}-rg{rg_label(rg)}" for lv in F6_LEVELS for rg in F6_ROW_GROUPS]
