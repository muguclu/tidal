"""F2: SQL Server clustered columnstore (the strongest relational option, fair comparison).

Rows are inserted in (instrument, time) order, then ``REBUILD`` with ``MAXDOP = 1`` compresses
the delta stores into rowgroups that keep that order, so segment elimination on ``ts_ns`` works.
The rebuild is part of the load (W1) time.
"""

from typing import ClassVar

from benchmarks.storage.formats.sql_f1 import SqlF1


class SqlF2(SqlF1):
    """Same columns and T-SQL as F1, clustered columnstore instead of rowstore."""

    id = "F2"
    time_cols: ClassVar[tuple[str, ...]] = ("ts_ns",)
    ddl = (
        "CREATE TABLE {t} (instrument_key SMALLINT NOT NULL, ts_ns BIGINT NOT NULL,"
        " price FLOAT NOT NULL, size REAL NOT NULL, side TINYINT NOT NULL,"
        " quality TINYINT NOT NULL, ingested_at_ns BIGINT NOT NULL, version SMALLINT NOT NULL);"
        " CREATE CLUSTERED COLUMNSTORE INDEX cci ON {t}"
    )
    post_load = ("ALTER INDEX cci ON {t} REBUILD WITH (MAXDOP = 1)",)
