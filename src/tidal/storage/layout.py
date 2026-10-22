"""The only place blob paths are built or parsed (storage-layout §2, §5).

Every function returns a container-qualified path such as ``marketdata/v1/ticks/...``; the
first segment is the container, the rest is the blob name. Local dev mirrors the tree under
``./.data/``.
"""

# Error names follow the problem slugs, not the "...Error" suffix convention (as in common.errors).
# ruff: noqa: N818, PLR2004

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

from tidal.common.errors import TidalError
from tidal.contracts.paths import backtest_prefix as _backtest_prefix

CONTAINER_MARKETDATA: Final = "marketdata"
CONTAINER_RAW: Final = "raw"
CONTAINER_REFERENCE: Final = "reference"
MD_VERSION: Final = "v1"
DATASETS: Final = ("ticks", "bars_1s", "bars_1m", "bars_1h", "bars_1d")
DAILY_DATASET: Final = "bars_1d"
RAW_SOURCES: Final = ("eia", "fred")
MANIFEST_NAME: Final = "_manifest.json"

_INSTRUMENT = re.compile(r"[A-Z0-9]+\.[A-Z0-9_]+\.[A-Z0-9_]+")
_SERIES = re.compile(r"[a-z0-9_]+")
_MD = re.compile(
    rf"{CONTAINER_MARKETDATA}/{MD_VERSION}/(?P<dataset>[a-z0-9_]+)/instrument=(?P<instrument>[^/]+)"
    r"/year=(?P<year>\d{4})/month=(?P<month>\d{2})/(?:part-(?P<seq>\d{5})\.parquet|_manifest\.json)"
)
_RAW = re.compile(
    r"raw/(?P<source>[a-z]+)/(?P<series>[a-z0-9_]+)/(?P<day>\d{8})(?:-(?P<digest>[0-9a-f]{8}))?\.json\.gz"
)


class InvalidPath(TidalError):
    """A blob path or one of its parts breaks the storage-layout rules."""

    status = 500
    slug = "invalid-path"
    title = "Invalid storage path"


@dataclass(frozen=True, slots=True)
class MdPart:
    """Parsed market-data blob; ``seq`` is ``None`` for the manifest."""

    dataset: str
    instrument_id: str
    year: int
    month: int
    seq: int | None


def _check_partition(dataset: str, instrument_id: str, year: int, month: int) -> None:
    if dataset not in DATASETS:
        raise InvalidPath(f"unknown dataset {dataset!r}")
    if not _INSTRUMENT.fullmatch(instrument_id):
        raise InvalidPath(f"bad instrument_id {instrument_id!r}")
    if not 1000 <= year <= 9999:
        raise InvalidPath(f"year {year} must have four digits")
    if (month != 0) if dataset == DAILY_DATASET else (not 1 <= month <= 12):
        raise InvalidPath(f"month {month} invalid for {dataset} (daily bars use 0, others 1-12)")


def md_partition_prefix(dataset: str, instrument_id: str, year: int, month: int) -> str:
    """Directory prefix (with trailing slash) of one partition; daily bars use ``month=00``."""
    _check_partition(dataset, instrument_id, year, month)
    return (
        f"{CONTAINER_MARKETDATA}/{MD_VERSION}/{dataset}/instrument={instrument_id}"
        f"/year={year:04d}/month={month:02d}/"
    )


def md_part_path(dataset: str, instrument_id: str, year: int, month: int, seq: int) -> str:
    """Path of ``part-{seq:05d}.parquet`` inside a partition."""
    if not 0 <= seq <= 99999:
        raise InvalidPath(f"seq {seq} out of range")
    return f"{md_partition_prefix(dataset, instrument_id, year, month)}part-{seq:05d}.parquet"


def md_manifest_path(dataset: str, instrument_id: str, year: int, month: int) -> str:
    """Path of the partition's ``_manifest.json`` (SPEC-03 §2)."""
    return md_partition_prefix(dataset, instrument_id, year, month) + MANIFEST_NAME


def parse_md_path(path: str) -> MdPart:
    """Inverse of :func:`md_part_path` / :func:`md_manifest_path`; validates all rules."""
    m = _MD.fullmatch(path)
    if m is None:
        raise InvalidPath(f"not a market-data path: {path!r}")
    part = MdPart(
        m["dataset"],
        m["instrument"],
        int(m["year"]),
        int(m["month"]),
        None if m["seq"] is None else int(m["seq"]),
    )
    _check_partition(part.dataset, part.instrument_id, part.year, part.month)
    return part


def raw_path(source: str, series: str, day: date, digest: str | None = None) -> str:
    """``raw/{source}/{series}/{yyyymmdd}[-{sha8}].json.gz``; the series is lower-cased (§5).

    ``digest`` (first 8 hex chars of the payload sha256) makes restated responses for the same day
    distinct, immutable blobs instead of a silent no-op on the create-only store.
    """
    series = series.lower()
    if source not in RAW_SOURCES or not _SERIES.fullmatch(series):
        raise InvalidPath(f"bad raw source/series {source!r}/{series!r}")
    if digest is not None and not re.fullmatch(r"[0-9a-f]{8}", digest):
        raise InvalidPath(f"bad raw digest {digest!r}")
    suffix = f"-{digest}" if digest is not None else ""
    return f"{CONTAINER_RAW}/{source}/{series}/{day:%Y%m%d}{suffix}.json.gz"


def parse_raw_path(path: str) -> tuple[str, str, date]:
    """Inverse of :func:`raw_path` returning ``(source, series, day)``."""
    m = _RAW.fullmatch(path)
    if m is None or m["source"] not in RAW_SOURCES:
        raise InvalidPath(f"not a raw path: {path!r}")
    try:
        day = date(int(m["day"][:4]), int(m["day"][4:6]), int(m["day"][6:]))
    except ValueError:
        raise InvalidPath(f"bad date in {path!r}") from None
    return m["source"], m["series"], day


def reference_path(name: str = "instruments") -> str:
    """``reference/{name}.parquet`` (default: the instrument table)."""
    if not _SERIES.fullmatch(name):
        raise InvalidPath(f"bad reference name {name!r}")
    return f"{CONTAINER_REFERENCE}/{name}.parquet"


def backtest_prefix(job_id: str, child_index: int | None = None) -> str:
    """``backtests/bt_<ULID>/`` or ``.../child-NNNN/`` (built in ``tidal.contracts.paths``)."""
    try:
        return _backtest_prefix(job_id, child_index)
    except ValueError as exc:
        raise InvalidPath(str(exc)) from exc


def split_container(path: str) -> tuple[str, str]:
    """Split a container-qualified path into ``(container, blob_name)``."""
    container, sep, name = path.partition("/")
    if not sep or not name:
        raise InvalidPath(f"path has no container: {path!r}")
    return container, name
