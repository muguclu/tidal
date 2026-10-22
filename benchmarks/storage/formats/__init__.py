"""Storage-format candidates for the SPEC-02 benchmark; one module per family.

``ALL_FORMATS`` is the local registry used by the runner (F0-F2 need a SQL Server from the
environment, see ``sql_base.SqlTarget``). :func:`file_formats` with a :class:`BlobTarget` rebinds
F3-F6 to Azurite/Blob, so requests and transferred bytes are counted for real (``file_base``).
"""

from collections.abc import Callable
from functools import partial
from pathlib import Path

from benchmarks.storage.formats.file_base import BlobTarget
from benchmarks.storage.formats.file_f3 import F3Format
from benchmarks.storage.formats.file_f4 import F4Format
from benchmarks.storage.formats.file_f5 import (
    F6_DEFAULT,
    F6_LEVELS,
    F6_ROW_GROUPS,
    F5Format,
    f6_ids,
    rg_label,
)
from benchmarks.storage.formats.sql_base import SqlFormat
from benchmarks.storage.formats.sql_f0 import SqlF0
from benchmarks.storage.formats.sql_f1 import SqlF1
from benchmarks.storage.formats.sql_f2 import SqlF2
from benchmarks.storage.workloads import Format

SQL_FORMATS: dict[str, type[SqlFormat]] = {"F0": SqlF0, "F1": SqlF1, "F2": SqlF2}


def file_formats(target: BlobTarget | None = None) -> dict[str, Callable[[Path], Format]]:
    """F3-F6 factories on the local file system, or on Azurite/Blob when ``target`` is given.

    The bare ``F6`` is one representative grid point (zstd 9, 1M rows); the full 3 x 3 sweep is
    registered as ``F6-z{3,6,9}-rg{128k,1m,4m}`` (:func:`f6_ids`).
    """
    out: dict[str, Callable[[Path], Format]] = {
        "F3": partial(F3Format, target=target),
        "F4": partial(F4Format, target=target),
        "F5": partial(F5Format, target=target),
        "F6": partial(
            F5Format, target=target, level=F6_DEFAULT[0], row_group=F6_DEFAULT[1], sweep=True
        ),
    }
    for lv in F6_LEVELS:
        for rg in F6_ROW_GROUPS:
            out[f"F6-z{lv}-rg{rg_label(rg)}"] = partial(
                F5Format, target=target, level=lv, row_group=rg, sweep=True
            )
    return out


FILE_FORMATS = file_formats()
ALL_FORMATS: dict[str, Callable[[Path], Format]] = {**SQL_FORMATS, **FILE_FORMATS}

__all__ = ["ALL_FORMATS", "FILE_FORMATS", "SQL_FORMATS", "f6_ids", "file_formats"]
