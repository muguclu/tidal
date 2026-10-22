"""Storage-format candidates for the SPEC-02 benchmark; one module per family."""

from benchmarks.storage.formats.sql_base import SqlFormat
from benchmarks.storage.formats.sql_f0 import SqlF0
from benchmarks.storage.formats.sql_f1 import SqlF1
from benchmarks.storage.formats.sql_f2 import SqlF2

SQL_FORMATS: dict[str, type[SqlFormat]] = {"F0": SqlF0, "F1": SqlF1, "F2": SqlF2}
