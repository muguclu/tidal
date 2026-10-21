"""Arrow schemas, quality flags, interval codes (data-contracts §2-6, storage-layout §3)."""

from enum import IntEnum, IntFlag, StrEnum
from typing import Final

import pyarrow as pa


class QualityFlag(IntFlag):
    """Quality bit flags stored in the int8 ``quality`` column (data-contracts §5)."""

    NEGATIVE = 1 << 0
    OUTLIER = 1 << 1
    GAP_BEFORE = 1 << 2
    RESTATED = 1 << 3
    SYNTHETIC = 1 << 4


#: Bits 5-7 are reserved and must be 0.
QUALITY_RESERVED_MASK: Final = 0b1110_0000
#: Largest valid ``quality`` value (all defined flags set).
QUALITY_MAX: Final = sum(flag.value for flag in QualityFlag)


def validate_quality(value: int) -> QualityFlag:
    """Return ``value`` as a ``QualityFlag``.

    Raises:
        ValueError: if ``value`` is negative or sets a reserved bit.
    """
    if value < 0 or value & QUALITY_RESERVED_MASK or value > QUALITY_MAX:
        msg = f"quality {value} sets reserved bits (allowed 0..{QUALITY_MAX})"
        raise ValueError(msg)
    return QualityFlag(value)


class Interval(StrEnum):
    """Bar interval (data-contracts §3)."""

    S1 = "1s"
    M1 = "1m"
    H1 = "1h"
    D1 = "1d"


#: ``md.bar_hot.interval_code`` values (data-contracts §6).
INTERVAL_CODES: Final[dict[Interval, int]] = {
    Interval.S1: 1,
    Interval.M1: 2,
    Interval.H1: 3,
    Interval.D1: 4,
}


class TickSide(IntEnum):
    """Tick ``side`` codes stored as int8 (data-contracts §2)."""

    TRADE = 0
    BID = 1
    ASK = 2


_ID = pa.dictionary(pa.int16(), pa.string())


def _schema(name: str, *fields: "pa.Field[pa.DataType]") -> pa.Schema:
    return pa.schema(list(fields), metadata={"tidal.schema": name})


TICK_V1: Final = _schema(
    "TICK_V1",
    pa.field("ts_ns", pa.int64(), nullable=False),
    pa.field("instrument_id", _ID, nullable=False),
    pa.field("price", pa.float64(), nullable=False),
    pa.field("size", pa.float32(), nullable=False),
    pa.field("side", pa.int8(), nullable=False),
    pa.field("quality", pa.int8(), nullable=False),
    pa.field("ingested_at_ns", pa.int64(), nullable=False),
    pa.field("version", pa.int16(), nullable=False),
)

BAR_V1: Final = _schema(
    "BAR_V1",
    pa.field("ts_ns", pa.int64(), nullable=False),
    pa.field("instrument_id", _ID, nullable=False),
    pa.field("interval", pa.dictionary(pa.int8(), pa.string()), nullable=False),
    *(pa.field(name, pa.float64(), nullable=False) for name in ("open", "high", "low", "close")),
    pa.field("volume", pa.float64(), nullable=False),  # NaN = no volume (§3.1)
    pa.field("trade_count", pa.int32(), nullable=True),
    pa.field("quality", pa.int8(), nullable=False),
    pa.field("ingested_at_ns", pa.int64(), nullable=False),
    pa.field("version", pa.int16(), nullable=False),
)

BT_EQUITY_V1: Final = _schema(
    "BT_EQUITY_V1",
    pa.field("ts_ns", pa.int64(), nullable=False),
    *(pa.field(n, pa.float64(), nullable=False) for n in ("equity", "position", "drawdown")),
)

BT_TRADES_V1: Final = _schema(
    "BT_TRADES_V1",
    pa.field("ts_ns", pa.int64(), nullable=False),
    pa.field("instrument_id", pa.string(), nullable=False),
    *(pa.field(n, pa.float64(), nullable=False) for n in ("qty", "price", "fee", "slippage")),
)

SCHEMAS: Final[dict[str, pa.Schema]] = {
    "TICK_V1": TICK_V1,
    "BAR_V1": BAR_V1,
    "BT_EQUITY_V1": BT_EQUITY_V1,
    "BT_TRADES_V1": BT_TRADES_V1,
}


class ParquetEncoding(StrEnum):
    """Parquet column encodings named by the storage contract."""

    DELTA_BINARY_PACKED = "DELTA_BINARY_PACKED"
    BYTE_STREAM_SPLIT = "BYTE_STREAM_SPLIT"
    RLE_DICTIONARY = "RLE_DICTIONARY"


def _encodings(schema: pa.Schema) -> dict[str, ParquetEncoding]:
    """data-contracts §2 rules: ``*_ns`` delta, floats BSS, small ints/dictionaries RLE_DICTIONARY.

    Columns not covered (``trade_count``) are absent and use the writer default.
    """
    out: dict[str, ParquetEncoding] = {}
    for f in schema:
        t = f.type
        if f.name.endswith("_ns"):
            out[f.name] = ParquetEncoding.DELTA_BINARY_PACKED
        elif pa.types.is_floating(t):
            out[f.name] = ParquetEncoding.BYTE_STREAM_SPLIT
        elif (
            pa.types.is_dictionary(t)
            or pa.types.is_int8(t)
            or pa.types.is_int16(t)
            or f.name == "instrument_id"  # plain string in BT_TRADES_V1; dictionary on (§3)
        ):
            out[f.name] = ParquetEncoding.RLE_DICTIONARY
    return out


#: Per-schema column -> encoding intent (storage-layout §3, data-contracts §2).
PARQUET_ENCODINGS: Final[dict[str, dict[str, ParquetEncoding]]] = {
    name: _encodings(schema) for name, schema in SCHEMAS.items()
}
