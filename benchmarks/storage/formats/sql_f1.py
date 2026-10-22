"""F1: SQL Server rowstore, ``ts_ns BIGINT``, PAGE compression (the tuned relational option)."""

from typing import Any, ClassVar

import numpy as np

from benchmarks.storage.formats.sql_base import NS_DAY, SqlFormat


class SqlF1(SqlFormat):
    """``md.tick_hot`` layout: clustered PK (instrument_key, ts_ns, side, version), PAGE."""

    id = "F1"
    time_cols: ClassVar[tuple[str, ...]] = ("ts_ns",)
    ns_expr = "ts_ns"
    day_expr = f"ts_ns / CAST({NS_DAY} AS BIGINT)"  # a bare literal would be NUMERIC
    ddl = (
        "CREATE TABLE {t} (instrument_key SMALLINT NOT NULL, ts_ns BIGINT NOT NULL,"
        " price FLOAT NOT NULL, size REAL NOT NULL, side TINYINT NOT NULL,"
        " quality TINYINT NOT NULL, ingested_at_ns BIGINT NOT NULL, version SMALLINT NOT NULL,"
        " CONSTRAINT {pk} PRIMARY KEY CLUSTERED (instrument_key, ts_ns, side, version))"
        " WITH (DATA_COMPRESSION = PAGE)"
    )

    def _time_values(self, ts: "np.typing.NDArray[np.int64]") -> list[list[Any]]:
        return [ts.tolist()]

    def _range(self, lo: int, hi: int) -> tuple[str, list[Any]]:
        return "ts_ns >= ? AND ts_ns < ?", [lo, hi]
