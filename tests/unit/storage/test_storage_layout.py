"""Path builder: examples, rules and hypothesis round trips."""

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tidal.storage import layout

IDS = st.from_regex(r"[A-Z0-9]{1,6}\.[A-Z0-9_]{1,8}\.[A-Z0-9_]{1,8}", fullmatch=True)
YEARS = st.integers(1000, 9999)


def test_examples() -> None:
    assert (
        layout.md_part_path("ticks", "SYN.BRENT.TICK", 2024, 3, 7)
        == "marketdata/v1/ticks/instrument=SYN.BRENT.TICK/year=2024/month=03/part-00007.parquet"
    )
    assert layout.md_part_path("bars_1d", "EIA.WTI.SPOT", 2020, 0, 0).endswith(
        "year=2020/month=00/part-00000.parquet"
    )
    assert layout.md_manifest_path("bars_1m", "A.B.C", 2001, 12).endswith("month=12/_manifest.json")
    assert layout.raw_path("eia", "RWTC", date(2020, 4, 20)) == "raw/eia/rwtc/20200420.json.gz"
    assert layout.reference_path() == "reference/instruments.parquet"
    job = "bt_" + "0" * 26
    assert layout.backtest_prefix(job, 3) == f"backtests/{job}/child-0003/"
    assert layout.backtest_prefix(job) == f"backtests/{job}/"
    assert layout.split_container("marketdata/v1/x") == ("marketdata", "v1/x")


@given(st.sampled_from(layout.DATASETS[:-1]), IDS, YEARS, st.integers(1, 12), st.integers(0, 99999))
def test_md_round_trip(dataset: str, inst: str, year: int, month: int, seq: int) -> None:
    path = layout.md_part_path(dataset, inst, year, month, seq)
    assert layout.parse_md_path(path) == layout.MdPart(dataset, inst, year, month, seq)


@given(IDS, YEARS, st.integers(0, 99999))
def test_daily_round_trip_uses_month_zero(inst: str, year: int, seq: int) -> None:
    path = layout.md_part_path("bars_1d", inst, year, 0, seq)
    assert layout.parse_md_path(path) == layout.MdPart("bars_1d", inst, year, 0, seq)


@given(st.sampled_from(layout.DATASETS), IDS, YEARS)
def test_manifest_round_trip(dataset: str, inst: str, year: int) -> None:
    month = 0 if dataset == "bars_1d" else 1
    part = layout.parse_md_path(layout.md_manifest_path(dataset, inst, year, month))
    assert part == layout.MdPart(dataset, inst, year, month, None)


@given(
    st.sampled_from(layout.RAW_SOURCES),
    st.from_regex(r"[a-z0-9_]{1,12}", fullmatch=True),
    st.dates(min_value=date(1000, 1, 1)),
)
def test_raw_round_trip(source: str, series: str, day: date) -> None:
    assert layout.parse_raw_path(layout.raw_path(source, series, day)) == (source, series, day)


@pytest.mark.parametrize(
    "args",
    [
        ("nope", "A.B.C", 2020, 1),
        ("ticks", "a.b.c", 2020, 1),  # instrument values keep contract (upper) casing
        ("ticks", "A.B.C", 2020, 0),  # month 00 is for daily bars only
        ("ticks", "A.B.C", 2020, 13),
        ("bars_1d", "A.B.C", 2020, 1),  # daily bars are yearly
        ("ticks", "A.B.C", 99, 1),
    ],
)
def test_builder_rejects(args: tuple[str, str, int, int]) -> None:
    with pytest.raises(layout.InvalidPath):
        layout.md_partition_prefix(*args)


@pytest.mark.parametrize(
    "path",
    [
        "marketdata/v1/ticks/instrument=A.B.C/year=2020/month=01/part-1.parquet",
        "marketdata/v2/ticks/instrument=A.B.C/year=2020/month=01/part-00001.parquet",
        "marketdata/v1/Ticks/instrument=A.B.C/year=2020/month=01/part-00001.parquet",
        "marketdata/v1/ticks/instrument=a.b.c/year=2020/month=01/part-00001.parquet",
        "marketdata/v1/bars_1d/instrument=A.B.C/year=2020/month=05/part-00001.parquet",
        "garbage",
    ],
)
def test_parser_rejects(path: str) -> None:
    with pytest.raises(layout.InvalidPath):
        layout.parse_md_path(path)


def test_other_validators() -> None:
    with pytest.raises(layout.InvalidPath):
        layout.parse_raw_path("raw/eia/RWTC/20200420.json.gz")  # series must be lower case
    with pytest.raises(layout.InvalidPath):
        layout.raw_path("xxx", "a", date(2020, 1, 1))
    with pytest.raises(layout.InvalidPath):
        layout.reference_path("Bad Name")
    with pytest.raises(layout.InvalidPath):
        layout.backtest_prefix("bt_x")
    with pytest.raises(layout.InvalidPath):
        layout.backtest_prefix("bt_" + "0" * 26, 10000)
    with pytest.raises(layout.InvalidPath):
        layout.md_part_path("ticks", "A.B.C", 2020, 1, 100000)
    with pytest.raises(layout.InvalidPath):
        layout.split_container("nocontainer")
    with pytest.raises(layout.InvalidPath):
        layout.parse_raw_path("raw/eia/x/20201340.json.gz")
