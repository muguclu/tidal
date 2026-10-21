"""UTC epoch-nanosecond helpers and the injectable ``Clock`` (CLAUDE.md §7, config-contract).

Everything internal is an ``int64`` ``ts_ns``. Naive datetimes are rejected everywhere except
``exchange_local_to_utc_ns``, whose whole purpose is to interpret a *local* wall-clock reading.

This module is the only place allowed to read wall time (``SystemClock``); a test greps ``src/``.

Parsing of ``TimeParam`` strings (ISO-8601 ``...Z`` or epoch-ns digits) is delegated to
``tidal.contracts.models.to_ts_ns`` so there is a single source of truth for int64-bounded parsing.

DST policy for ``exchange_local_to_utc_ns`` (documented choice):

- **Overlap** (fall-back, local time occurs twice): the *first* occurrence (earlier UTC instant,
  i.e. still on daylight time) is used.
- **Gap** (spring-forward, local time does not exist): the reading is moved *forward by the gap*,
  i.e. interpreted with the offset in force before the transition (``01:30`` becomes ``02:30``
  local). Gaps are assumed to be 1 hour, which holds for every zone in the instrument catalogue.
- Time zone data comes from polars' bundled IANA database (no ``tzdata`` package needed on Windows).
"""

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal, Protocol, runtime_checkable

import polars as pl

from tidal.common.config import Settings
from tidal.contracts.models import INT64_MAX, INT64_MIN, datetime_to_ns
from tidal.contracts.models import to_ts_ns as _parse_time_param
from tidal.contracts.schemas import Interval

NS_PER_SECOND: Final = 1_000_000_000
#: Fixed-length intervals in ns (UTC epoch aligned; ``1d`` is a UTC day).
INTERVAL_NS: Final[Mapping[Interval, int]] = {
    Interval.S1: NS_PER_SECOND,
    Interval.M1: 60 * NS_PER_SECOND,
    Interval.H1: 3_600 * NS_PER_SECOND,
    Interval.D1: 86_400 * NS_PER_SECOND,
}
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NAIVE_ISO: Final = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?")


class NaiveDatetimeError(ValueError):
    """A timestamp without time zone information was supplied where an instant is required."""


def _check_range(ns: int) -> int:
    if not INT64_MIN <= ns <= INT64_MAX:
        msg = f"{ns} is outside the int64 epoch-ns range"
        raise ValueError(msg)
    return ns


def to_ts_ns(value: datetime | str) -> int:
    """Convert an aware ``datetime`` or a string to UTC epoch nanoseconds.

    Strings: the contract forms (``...Z`` ISO with up to 9 fraction digits, or epoch-ns digits) go
    through ``tidal.contracts.models.to_ts_ns``; other ISO-8601 strings with an explicit UTC offset
    (``+01:00``) are accepted at microsecond precision.

    Raises:
        NaiveDatetimeError: for a naive datetime or a string without offset.
        ValueError: for unparseable input or a result outside int64.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "naive datetime rejected; attach a time zone"
            raise NaiveDatetimeError(msg)
        return _check_range(datetime_to_ns(value))
    try:
        return _parse_time_param(value)
    except ValueError:
        if _NAIVE_ISO.fullmatch(value):
            msg = f"naive timestamp {value!r} rejected; use an explicit offset or Z"
            raise NaiveDatetimeError(msg) from None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            msg = f"unparseable timestamp {value!r}"
            raise ValueError(msg) from None
        return to_ts_ns(parsed)


def from_ts_ns(ts_ns: int) -> datetime:
    """Aware UTC ``datetime`` for ``ts_ns``; sub-microsecond digits are floored (lossy)."""
    return _EPOCH + timedelta(microseconds=ts_ns // 1_000)


def format_iso_ns(ts_ns: int) -> str:
    """ISO-8601 UTC string with exactly 9 fraction digits (``...:00.000000000Z``)."""
    seconds, nanos = divmod(ts_ns, NS_PER_SECOND)
    base = (_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{nanos:09d}Z"


def floor_to_interval(ts_ns: int, interval: Interval | str) -> int:
    """Floor ``ts_ns`` to the start of its ``1s``/``1m``/``1h``/``1d`` UTC-aligned interval."""
    step = INTERVAL_NS[Interval(interval)]
    return ts_ns - ts_ns % step  # Python % is non-negative for step > 0, so this floors


def month_bounds(year: int, month: int) -> tuple[int, int]:
    """Half-open ``[start_ns, end_ns)`` of a UTC calendar month."""
    if not 1 <= month <= 12:  # noqa: PLR2004 - calendar constant
        msg = f"month {month} out of range"
        raise ValueError(msg)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)  # noqa: PLR2004
    return (
        to_ts_ns(datetime(year, month, 1, tzinfo=UTC)),
        to_ts_ns(datetime(end_year, end_month, 1, tzinfo=UTC)),
    )


def year_bounds(year: int) -> tuple[int, int]:
    """Half-open ``[start_ns, end_ns)`` of a UTC calendar year."""
    return (
        to_ts_ns(datetime(year, 1, 1, tzinfo=UTC)),
        to_ts_ns(datetime(year + 1, 1, 1, tzinfo=UTC)),
    )


def partition_bounds(ts_ns: int, unit: Literal["month", "year"]) -> tuple[int, int]:
    """Half-open bounds of the UTC month or year partition containing ``ts_ns``."""
    moment = from_ts_ns(ts_ns)
    return month_bounds(moment.year, moment.month) if unit == "month" else year_bounds(moment.year)


def local_to_utc_ns_series(local: pl.Series, tz: str) -> pl.Series:
    """Vectorised ``exchange_local_to_utc_ns`` for a naive ``Datetime`` series (see DST policy).

    Returns an ``Int64`` series of UTC epoch ns. Raises ``ValueError`` for an unknown ``tz``.
    """
    naive = local.cast(pl.Datetime("ns"))
    try:
        first = naive.dt.replace_time_zone(tz, ambiguous="earliest", non_existent="null")
        if first.null_count():
            # Gap readings come back null: retry one hour later (post-transition offset), which
            # equals the original reading interpreted with the pre-transition offset.
            later = (naive + pl.duration(hours=1)).dt.replace_time_zone(
                tz, ambiguous="earliest", non_existent="null"
            )
            first = first.fill_null(later)
    except pl.exceptions.ComputeError as exc:
        msg = f"unknown or invalid time zone {tz!r}"
        raise ValueError(msg) from exc
    return first.dt.epoch("ns")


def exchange_local_to_utc_ns(local: str | date | datetime, tz: str) -> int:
    """UTC epoch ns of an exchange-local wall-clock reading (module docstring: DST policy).

    ``local`` is a naive ISO string, a ``date`` (midnight local) or a *naive* ``datetime``; an
    aware ``datetime`` or a string with an offset/``Z`` is rejected because it is not local.
    Resolution is microseconds.
    """
    if isinstance(local, str):
        if not _NAIVE_ISO.fullmatch(local):
            msg = f"expected a naive local ISO date/time, got {local!r}"
            raise ValueError(msg)
        parsed = datetime.fromisoformat(local)
    elif isinstance(local, datetime):
        if local.tzinfo is not None:
            msg = "exchange_local_to_utc_ns takes naive local time; use to_ts_ns for aware input"
            raise ValueError(msg)
        parsed = local
    else:
        parsed = datetime(local.year, local.month, local.day)  # noqa: DTZ001 - local by design
    series = pl.Series([parsed], dtype=pl.Datetime("ns"))
    return int(local_to_utc_ns_series(series, tz)[0])


@runtime_checkable
class Clock(Protocol):
    """Source of "now"; the only sanctioned way to read time in services."""

    def now_ns(self) -> int:
        """Current UTC epoch nanoseconds."""
        ...

    def now(self) -> datetime:
        """Current time as an aware UTC datetime."""
        ...


class SystemClock:
    """Wall clock. The only place in ``src/`` that reads real time."""

    def now_ns(self) -> int:
        """Current UTC epoch nanoseconds from the OS clock."""
        import time  # noqa: PLC0415 - keep wall-time access confined to this class

        return time.time_ns()

    def now(self) -> datetime:
        """Current aware UTC datetime."""
        return from_ts_ns(self.now_ns())


class FixedClock:
    """Frozen clock for tests and ``TIDAL_CLOCK__NOW``; ``advance`` moves it explicitly."""

    def __init__(self, now_ns: int) -> None:
        """Freeze the clock at ``now_ns`` (UTC epoch ns)."""
        self._now_ns = _check_range(now_ns)

    def now_ns(self) -> int:
        """The frozen UTC epoch nanoseconds."""
        return self._now_ns

    def now(self) -> datetime:
        """The frozen instant as an aware UTC datetime."""
        return from_ts_ns(self._now_ns)

    def advance(self, delta_ns: int) -> None:
        """Move the frozen instant forward (or back) by ``delta_ns``."""
        self._now_ns = _check_range(self._now_ns + delta_ns)


def clock_from_settings(settings: Settings) -> Clock:
    """``FixedClock`` at ``settings.clock.now`` when set (local only, validated by config)."""
    frozen = settings.clock.now
    return SystemClock() if frozen is None else FixedClock(to_ts_ns(frozen))
