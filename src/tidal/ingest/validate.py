"""Quality-flag validator for ``TICK_V1`` / ``BAR_V1`` tables (SPEC-01, data-contracts §5).

``validate_and_flag`` *sets flags and rejects only schema violations*; business-valid oddities
(negative prices, outliers, gaps, restatements) are kept and flagged. Values are never modified
(no clamping, no forward-fill). Existing ``quality`` bits are OR-ed into the result, never cleared.

Flag definitions (documented choices)
-------------------------------------
``NEGATIVE``
    ``price < 0`` (ticks) or ``min(open, high, low, close) < 0`` (bars).
``OUTLIER``
    Let ``x`` be the price series of one *series key* in time order (ticks: ``close``-like
    ``price`` per ``(instrument_id, side)``, so trades and quotes are not mixed; bars: ``close``
    per ``(instrument_id, interval)`` with one value per distinct ``ts_ns``, the highest version).
    Let ``d_i = x_i - x_{i-1}``. Price *differences* are used, not log-returns, because prices can
    be zero or negative. ``sigma_i`` is the sample standard deviation (ddof=1) of the trailing
    window ``d_{i-1000} .. d_{i-1}`` (the current difference is excluded so an outlier cannot
    inflate its own threshold). Row ``i`` is ``OUTLIER`` iff ``|d_i| > 8 * sigma_i``.
    Warm-up: ``sigma_i`` needs at least ``OUTLIER_MIN_PERIODS`` (30) prior differences, so the
    first 31 observations of a series are never flagged. A degenerate ``sigma`` (at most
    ``1e-10 * |x_i|``, e.g. a flat series) never flags. A spike also makes the *reversion* row's
    difference large, so the row after a spike is typically flagged as well. Windows do not span
    separate calls: validate in chunks large enough for the warm-up to matter little.
``GAP_BEFORE``
    Only for ``1d`` bars: at least one Monday-Friday date lies strictly between the previous
    distinct bar date and this one, using the *exchange-local* date (``exchange_tz`` mapping,
    UTC when absent). No holiday calendar (out of scope), so holidays are flagged as gaps, and
    weekends are never expected. The first row of a series never carries it. Ticks and bars of
    other intervals are not gap-checked here.
``RESTATED``
    ``version > 1``.
``SYNTHETIC``
    Set on every row when the caller passes ``synthetic=True`` (SYN sources).

Schema violations (``SchemaViolation``): wrong schema, nulls in non-nullable columns, non-finite
prices, ``size < 0`` or NaN, ``side`` outside 0..2, ``high < low`` or open/close outside
``[low, high]`` (data-contracts §3), ``volume < 0``, unknown ``interval``, ``version < 1``,
reserved quality bits set, ``ts_ns`` decreasing within an instrument (ticks) or
``(instrument, interval)`` (bars). Memory is dominated by one polars frame of the needed columns.
"""

from collections.abc import Mapping
from typing import Final

import polars as pl
import pyarrow as pa

from tidal.common.errors import ValidationFailed
from tidal.contracts.schemas import BAR_V1, TICK_V1, Interval, QualityFlag

OUTLIER_WINDOW: Final = 1000
OUTLIER_MIN_PERIODS: Final = 30
OUTLIER_SIGMAS: Final = 8.0
_SIGMA_FLOOR_REL: Final = 1e-10
_RESERVED: Final = 0b1110_0000
_SIDES: Final = (0, 1, 2)
_NS: Final = pl.Datetime("ns")


class SchemaViolation(ValidationFailed):
    """Input table violates the TICK_V1/BAR_V1 contract; nothing is written (SPEC-01)."""

    slug = "schema-violation"
    title = "Schema violation"


def _same_layout(table: pa.Table, schema: pa.Schema) -> bool:
    return [(f.name, f.type) for f in table.schema] == [(f.name, f.type) for f in schema]


def _detect(table: pa.Table) -> pa.Schema:
    for schema in (TICK_V1, BAR_V1):
        if _same_layout(table, schema):
            return schema
    msg = "table does not match TICK_V1 or BAR_V1 (column names, order and types)"
    raise SchemaViolation(msg)


def _raise_if_any(df: pl.DataFrame, checks: Mapping[str, pl.Expr]) -> None:
    counts = df.select([expr.sum().alias(name) for name, expr in checks.items()]).row(0, named=True)
    bad = [f"{name} ({n} rows)" for name, n in counts.items() if n]
    if bad:
        msg = "schema violations: " + "; ".join(bad)
        raise SchemaViolation(msg)


def _common_checks(keys: list[str]) -> dict[str, pl.Expr]:
    quality = pl.col("quality").cast(pl.Int16)
    return {
        "version < 1": pl.col("version") < 1,
        "reserved quality bits set": (quality & _RESERVED) != 0,
        "ts_ns decreasing within " + "/".join(keys): pl.col("ts_ns").diff().over(keys) < 0,
    }


def _outlier(x: pl.Expr, keys: list[str]) -> pl.Expr:
    """Boolean expression: |diff| > 8 * trailing-1000 sigma (see module docstring)."""
    diff = x.diff().over(keys)
    sigma = (
        x.diff().shift(1).rolling_std(OUTLIER_WINDOW, min_samples=OUTLIER_MIN_PERIODS).over(keys)
    )
    flagged = (diff.abs() > OUTLIER_SIGMAS * sigma) & (sigma > _SIGMA_FLOOR_REL * x.abs())
    return flagged.fill_null(value=False)


def _local_date(exchange_tz: Mapping[str, str]) -> pl.Expr:
    utc = pl.col("ts_ns").cast(_NS).dt.replace_time_zone("UTC")
    expr = utc.dt.date()
    for instrument, tz in exchange_tz.items():
        expr = (
            pl.when(pl.col("instrument_id") == instrument)
            .then(utc.dt.convert_time_zone(tz).dt.date())
            .otherwise(expr)
        )
    return expr


def _flag_bits(**flags: pl.Expr) -> pl.Expr:
    bits = pl.lit(0, dtype=pl.Int16)
    for name, expr in flags.items():
        bits = bits | (expr.cast(pl.Int16) * int(QualityFlag[name.upper()]))
    return bits


def _validate_ticks(df: pl.DataFrame, *, synthetic: bool) -> pl.Expr:
    _raise_if_any(
        df,
        {
            "non-finite price": ~pl.col("price").is_finite(),
            "size negative or NaN": ~(pl.col("size") >= 0),
            "side not in 0..2": ~pl.col("side").is_in(_SIDES),
            **_common_checks(["instrument_id"]),
        },
    )
    return _flag_bits(
        negative=pl.col("price") < 0,
        outlier=_outlier(pl.col("price"), ["instrument_id", "side"]),
        restated=pl.col("version") > 1,
        synthetic=pl.lit(synthetic),
    )


def _validate_bars(
    df: pl.DataFrame, *, synthetic: bool, exchange_tz: Mapping[str, str]
) -> tuple[pl.DataFrame, pl.Expr]:
    df = df.with_columns(
        pl.col("instrument_id").cast(pl.String), pl.col("interval").cast(pl.String)
    )
    ohlc = ["open", "high", "low", "close"]
    low, high = pl.col("low"), pl.col("high")
    _raise_if_any(
        df,
        {
            "non-finite open/high/low/close": pl.any_horizontal(~pl.col(ohlc).is_finite()),
            "high < low": high < low,
            "low above open/close": low > pl.min_horizontal("open", "close"),
            "high below open/close": high < pl.max_horizontal("open", "close"),
            "volume negative": pl.col("volume") < 0,
            "unknown interval": ~pl.col("interval").is_in([i.value for i in Interval]),
            **_common_checks(["instrument_id", "interval"]),
        },
    )
    keys = ["instrument_id", "interval"]
    series = (
        df.sort("version")
        .group_by([*keys, "ts_ns"], maintain_order=True)
        .agg(pl.col("close").last())
        .sort([*keys, "ts_ns"])
    )
    try:
        date = _local_date(exchange_tz)
        prev = date.shift(1).over(keys)
        series = series.with_columns(
            _outlier(pl.col("close"), keys).alias("_outlier"),
            (
                (pl.col("interval") == Interval.D1.value)
                & (pl.business_day_count(prev + pl.duration(days=1), date) > 0)
            )
            .fill_null(value=False)
            .alias("_gap"),
        )
    except pl.exceptions.ComputeError as exc:
        msg = "exchange_tz contains an unknown time zone"
        raise ValueError(msg) from exc
    df = df.join(
        series.select([*keys, "ts_ns", "_outlier", "_gap"]),
        on=[*keys, "ts_ns"],
        how="left",
        maintain_order="left",
    )
    bits = _flag_bits(
        negative=pl.min_horizontal(ohlc) < 0,
        outlier=pl.col("_outlier"),
        gap_before=pl.col("_gap"),
        restated=pl.col("version") > 1,
        synthetic=pl.lit(synthetic),
    )
    return df, bits


def validate_and_flag(
    table: pa.Table,
    *,
    synthetic: bool = False,
    exchange_tz: Mapping[str, str] | None = None,
) -> pa.Table:
    """Return ``table`` with ``quality`` flags set; all other columns are untouched.

    Args:
        table: a ``TICK_V1`` or ``BAR_V1`` table, sorted by ``ts_ns`` within each instrument.
        synthetic: set ``SYNTHETIC`` on all rows (caller knows the source is ``SYN``).
        exchange_tz: ``instrument_id -> IANA tz`` used to find the local weekday of ``1d`` bars
            for ``GAP_BEFORE``; instruments not listed use UTC dates.

    Raises:
        SchemaViolation: on any contract violation (see module docstring).
        ValueError: if ``exchange_tz`` holds an unknown time zone.
    """
    schema = _detect(table)
    nulls = [f.name for f in schema if not f.nullable and table.column(f.name).null_count]
    if nulls:
        msg = f"nulls in non-nullable columns: {', '.join(nulls)}"
        raise SchemaViolation(msg)
    df = pl.from_arrow(table.select([f.name for f in schema if f.name != "trade_count"]))
    assert isinstance(df, pl.DataFrame)  # noqa: S101 - from_arrow(Table) is always a DataFrame
    if schema is TICK_V1:
        bits = _validate_ticks(df, synthetic=synthetic)
    else:
        df, bits = _validate_bars(df, synthetic=synthetic, exchange_tz=exchange_tz or {})
    quality = df.select((pl.col("quality").cast(pl.Int16) | bits).cast(pl.Int8))["quality"]
    index = table.schema.get_field_index("quality")
    return table.set_column(index, schema.field("quality"), quality.to_arrow())
