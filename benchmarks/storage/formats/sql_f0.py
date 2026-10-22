"""F0: SQL Server rowstore, ``DATETIME2(7)`` time, no compression (the naive baseline).

Same shape as ``md.tick_hot`` except the time type and the compression (SPEC-02 section 2).
``DATETIME2(7)`` resolves 100 ns while the synthetic ticks carry nanoseconds, so the sub-100 ns
remainder is kept in ``ts_sub`` (TINYINT, 1 byte/row) to return bit-identical ``ts_ns`` and keep
the key unique. That byte is charged to F0; see the T-202 report for the deviation note.
"""

from typing import Any, ClassVar

import numpy as np

from benchmarks.storage.formats.sql_base import SqlFormat, dt2_text
from tidal.common.errors import ConfigError

_DT2 = "CAST(? AS DATETIME2(7))"


class SqlF0(SqlFormat):
    """DATETIME2 + FLOAT rowstore without compression."""

    id = "F0"
    time_cols: ClassVar[tuple[str, ...]] = ("ts", "ts_sub")
    ns_expr = "DATEDIFF_BIG(NANOSECOND, '1970-01-01', ts) + ts_sub"
    day_expr = "CAST(ts AS DATE)"
    ddl = (
        "CREATE TABLE {t} (instrument_key SMALLINT NOT NULL, ts DATETIME2(7) NOT NULL,"
        " ts_sub TINYINT NOT NULL, price FLOAT NOT NULL, size REAL NOT NULL, side TINYINT NOT NULL,"
        " quality TINYINT NOT NULL, ingested_at_ns BIGINT NOT NULL, version SMALLINT NOT NULL,"
        " CONSTRAINT {pk} PRIMARY KEY CLUSTERED (instrument_key, ts, ts_sub, side, version))"
        " WITH (DATA_COMPRESSION = NONE)"
    )

    def _time_values(self, ts: "np.typing.NDArray[np.int64]") -> list[list[Any]]:
        return [dt2_text(ts), (ts % 100).tolist()]

    def _range(self, lo: int, hi: int) -> tuple[str, list[Any]]:
        if lo % 100 or hi % 100:
            raise ConfigError("F0 range bounds must be multiples of 100 ns (DATETIME2(7) tick)")
        return f"ts >= {_DT2} AND ts < {_DT2}", [
            dt2_text(np.array([lo]))[0],
            dt2_text(np.array([hi]))[0],
        ]
