import tomllib
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tidal.common import timeutil
from tidal.common.config import Settings
from tidal.common.timeutil import (
    Clock,
    FixedClock,
    NaiveDatetimeError,
    SystemClock,
    clock_from_settings,
    exchange_local_to_utc_ns,
    floor_to_interval,
    format_iso_ns,
    from_ts_ns,
    month_bounds,
    partition_bounds,
    to_ts_ns,
    year_bounds,
)
from tidal.contracts.models import INT64_MAX, INT64_MIN
from tidal.contracts.schemas import Interval

H = 3_600 * 1_000_000_000
LONDON = "Europe/London"
NEW_YORK = "America/New_York"
int64s = st.integers(INT64_MIN, INT64_MAX)


def utc(year: int, month: int, day: int, *hms: int) -> int:
    hour, minute, second = (*hms, 0, 0, 0)[:3]
    return to_ts_ns(datetime(year, month, day, hour, minute, second, tzinfo=UTC))


# ---- parsing / formatting ----------------------------------------------------------------------


@given(int64s)
def test_iso_ns_roundtrip(ns: int) -> None:
    assert to_ts_ns(format_iso_ns(ns)) == ns


@given(st.integers(INT64_MIN + 1_000, INT64_MAX))  # flooring INT64_MIN would underflow
def test_from_ts_ns_floors_to_microseconds(ns: int) -> None:
    moment = from_ts_ns(ns)
    assert moment.tzinfo is UTC
    assert to_ts_ns(moment) == ns - ns % 1_000


def test_format_iso_ns_fixed_precision() -> None:
    assert format_iso_ns(utc(2020, 4, 20, 19, 30) + 7) == "2020-04-20T19:30:00.000000007Z"
    assert format_iso_ns(0) == "1970-01-01T00:00:00.000000000Z"
    assert format_iso_ns(-1) == "1969-12-31T23:59:59.999999999Z"


def test_to_ts_ns_inputs() -> None:
    assert to_ts_ns("1970-01-01T00:00:01Z") == 1_000_000_000
    assert to_ts_ns("123456789") == 123456789
    assert to_ts_ns("2021-01-01T01:00:00+01:00") == utc(2021, 1, 1)
    assert to_ts_ns(datetime(2021, 1, 1, 1, tzinfo=timezone(timedelta(hours=1)))) == utc(2021, 1, 1)


@pytest.mark.parametrize("bad", ["2021-01-01T00:00:00", "2021-01-01", "2021-01-01 10:00"])
def test_naive_strings_raise(bad: str) -> None:
    with pytest.raises(NaiveDatetimeError):
        to_ts_ns(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2021-01-01T01:00:00.123456789+01:00", utc(2021, 1, 1) + 123_456_789),
        ("2021-01-01T00:00:00.5-05:30", utc(2021, 1, 1, 5, 30) + 500_000_000),
        ("2021-01-01T01:00:00.000000001+0100", utc(2021, 1, 1) + 1),
        ("2021-01-01T01:00+01:00", utc(2021, 1, 1)),
    ],
)
def test_offset_strings_keep_nanoseconds(text: str, expected: int) -> None:
    assert to_ts_ns(text) == expected


@given(st.integers(0, 999_999_999), st.integers(-14, 14))
def test_offset_string_matches_z_form(nanos: int, hours: int) -> None:
    local = datetime(2021, 6, 1, 12, tzinfo=UTC) + timedelta(hours=hours)
    sign = "-" if hours < 0 else "+"
    text = f"{local:%Y-%m-%dT%H:%M:%S}.{nanos:09d}{sign}{abs(hours):02d}:00"
    assert to_ts_ns(text) == utc(2021, 6, 1, 12) + nanos


def test_naive_datetime_raises() -> None:
    with pytest.raises(NaiveDatetimeError):
        to_ts_ns(datetime(2021, 1, 1))  # noqa: DTZ001 - the point of the test


@pytest.mark.parametrize(
    "bad",
    [
        "garbage",
        "99999999999999999999",
        "2021-13-01T00:00:00Z",
        "2021-13-01T00:00:00+01:00",
        "2021-01-01T00:00:00.1234567891+01:00",
    ],
)
def test_unparseable_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        to_ts_ns(bad)


# ---- floor / partitions ------------------------------------------------------------------------


@given(int64s, st.sampled_from(["1s", "1m", "1h", "1d"]))
def test_floor_properties(ns: int, interval: str) -> None:
    floored = floor_to_interval(ns, interval)
    step = timeutil.INTERVAL_NS[Interval(interval)]
    assert floored <= ns < floored + step
    assert floored % step == 0
    assert floor_to_interval(floored, interval) == floored  # idempotent


def test_floor_known_values() -> None:
    ts = utc(2020, 4, 20, 19, 31, 7) + 5
    assert floor_to_interval(ts, "1m") == utc(2020, 4, 20, 19, 31)
    assert floor_to_interval(ts, "1d") == utc(2020, 4, 20)
    assert floor_to_interval(-1, "1s") == -1_000_000_000


def test_month_and_year_bounds() -> None:
    assert month_bounds(2024, 2) == (utc(2024, 2, 1), utc(2024, 3, 1))
    assert month_bounds(2024, 12) == (utc(2024, 12, 1), utc(2025, 1, 1))
    assert year_bounds(2020) == (utc(2020, 1, 1), utc(2021, 1, 1))
    with pytest.raises(ValueError, match="month"):
        month_bounds(2024, 13)


@given(st.integers(0, 4_000_000_000 * 1_000_000_000))
def test_partition_contains_ts(ns: int) -> None:
    for unit in ("month", "year"):
        start, end = partition_bounds(ns, unit)
        assert start <= ns < end


# ---- exchange-local -> UTC ---------------------------------------------------------------------


def test_local_midnight_winter_and_summer() -> None:
    assert exchange_local_to_utc_ns("2021-01-15", LONDON) == utc(2021, 1, 15)
    assert exchange_local_to_utc_ns(date(2021, 7, 15), LONDON) == utc(2021, 7, 14, 23)
    assert exchange_local_to_utc_ns("2020-04-20", NEW_YORK) == utc(2020, 4, 20, 4)
    chicago = datetime(2020, 4, 20, 14, 30)  # noqa: DTZ001 - naive local by design
    assert exchange_local_to_utc_ns(chicago, "America/Chicago") == utc(2020, 4, 20, 19, 30)


@pytest.mark.parametrize(
    ("local", "tz", "expected"),
    [
        # Spring forward: 01:00-02:00 London / 02:00-03:00 New York do not exist -> shift +1h.
        ("2021-03-28T00:30", LONDON, utc(2021, 3, 28, 0, 30)),
        ("2021-03-28T01:30", LONDON, utc(2021, 3, 28, 1, 30)),
        ("2021-03-28T02:30", LONDON, utc(2021, 3, 28, 1, 30)),
        ("2021-03-14T02:30", NEW_YORK, utc(2021, 3, 14, 7, 30)),
        ("2021-03-14T03:30", NEW_YORK, utc(2021, 3, 14, 7, 30)),
        # Fall back: ambiguous readings take the first (daylight-time) occurrence.
        ("2021-10-31T01:30", LONDON, utc(2021, 10, 31, 0, 30)),
        ("2021-10-31T00:30", LONDON, utc(2021, 10, 30, 23, 30)),
        ("2021-10-31T02:30", LONDON, utc(2021, 10, 31, 2, 30)),
        ("2021-11-07T01:30", NEW_YORK, utc(2021, 11, 7, 5, 30)),
        ("2021-11-07T02:30", NEW_YORK, utc(2021, 11, 7, 7, 30)),
    ],
)
def test_dst_edges(local: str, tz: str, expected: int) -> None:
    assert exchange_local_to_utc_ns(local, tz) == expected


@pytest.mark.parametrize(
    ("tz", "start"),
    [
        (LONDON, utc(2021, 3, 28)),
        (LONDON, utc(2021, 10, 31)),
        (NEW_YORK, utc(2021, 3, 14)),
        (NEW_YORK, utc(2021, 11, 7)),
    ],
)
@given(offset=st.integers(0, 24 * 60))
def test_dst_roundtrip_utc_local_utc(tz: str, start: int, offset: int) -> None:
    """UTC -> local -> UTC is identity except inside the repeated (second-occurrence) hour."""
    t = start + offset * 60 * 1_000_000_000
    local = (
        pl.Series([t], dtype=pl.Datetime("ns", "UTC"))
        .dt.convert_time_zone(tz)
        .dt.replace_time_zone(None)
    )
    back = int(timeutil.local_to_utc_ns_series(local, tz)[0])
    if back != t:  # only the second pass through a repeated local hour may differ
        assert back == t - H


def test_local_to_utc_series_is_vectorised_and_monotone_off_dst() -> None:
    days = pl.datetime_range(date(2021, 1, 1), date(2021, 12, 31), "1d", eager=True)
    out = timeutil.local_to_utc_ns_series(days, NEW_YORK)
    assert len(out) == 365
    assert out.is_sorted()


def test_exchange_local_rejects_aware_and_unknown_tz() -> None:
    with pytest.raises(ValueError, match="naive"):
        exchange_local_to_utc_ns(datetime(2021, 1, 1, tzinfo=UTC), LONDON)
    with pytest.raises(ValueError, match="naive local"):
        exchange_local_to_utc_ns("2021-01-01T00:00:00Z", LONDON)
    with pytest.raises(ValueError, match="time zone"):
        exchange_local_to_utc_ns("2021-01-01", "Mars/Base")


# ---- clocks ------------------------------------------------------------------------------------


def test_fixed_clock() -> None:
    clock = FixedClock(utc(2025, 12, 31))
    assert clock.now_ns() == utc(2025, 12, 31)
    assert clock.now() == datetime(2025, 12, 31, tzinfo=UTC)
    clock.advance(H)
    assert clock.now_ns() == utc(2025, 12, 31, 1)
    with pytest.raises(ValueError, match="int64"):
        clock.advance(INT64_MAX)


def test_system_clock_is_close_to_real_time() -> None:
    assert isinstance(SystemClock(), Clock)
    assert abs(SystemClock().now().year - datetime.now(UTC).year) <= 1


def test_clock_from_settings() -> None:
    assert isinstance(clock_from_settings(Settings.from_env({})), SystemClock)
    frozen = clock_from_settings(Settings.from_env({"TIDAL_CLOCK__NOW": "2025-12-31T23:59:59Z"}))
    assert isinstance(frozen, FixedClock)
    assert frozen.now_ns() == utc(2025, 12, 31, 23, 59, 59)


def test_ruff_bans_wall_clock_calls_outside_timeutil() -> None:
    """ruff TID251 enforces 'only the Clock reads wall time' in CI; check it stays configured."""
    root = Path(timeutil.__file__).resolve().parents[3]
    config = tomllib.loads((root / "ruff.toml").read_text(encoding="utf-8"))
    banned = config["lint"]["flake8-tidy-imports"]["banned-api"]
    for api in ("datetime.datetime.now", "datetime.datetime.utcnow", "time.time", "time.time_ns"):
        assert api in banned
    assert config["lint"]["per-file-ignores"]["src/tidal/common/timeutil.py"] == ["TID251"]
