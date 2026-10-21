import gzip
from datetime import date
from pathlib import Path

import pytest

from tidal.common.errors import NotFound
from tidal.contracts.instruments import CATALOGUE_V1
from tidal.ingest.sources.base import (
    RawChunk,
    instrument_for,
    persist_raw,
    raw_blob_path,
    series_for,
    sha256_hex,
)
from tidal.storage.parquet_writer import LocalStore

RAW = "raw/eia/rwtc/20200420.json.gz"


def chunk(payload: bytes = b'{"a":1}') -> RawChunk:
    return RawChunk(
        "EIA",
        "RWTC",
        "EIA.WTI.SPOT",
        1,
        payload,
        "application/json",
        0,
        sha256_hex(payload),
        date(2020, 4, 20),
    )


def test_series_mapping_round_trips_whole_eia_catalogue() -> None:
    eia = [e for e in CATALOGUE_V1 if e.instrument_id.startswith("EIA.")]
    assert len(eia) == 11
    for e in eia:
        assert series_for(e.instrument_id) == e.source_series
        assert instrument_for("EIA", e.source_series) == e.instrument_id


def test_unknown_mapping_raises() -> None:
    with pytest.raises(NotFound):
        series_for("EIA.NOPE.SPOT")
    with pytest.raises(NotFound):
        instrument_for("EIA", "XXX")


def test_persist_raw_gzip_idempotent(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    c = chunk()
    assert raw_blob_path(c) == RAW
    assert persist_raw(c, store) is True
    assert persist_raw(c, store) is False  # path exists -> no new write
    assert gzip.decompress((tmp_path / RAW).read_bytes()) == c.payload
    assert store.list_paths("raw/eia/") == [RAW]


def test_persist_raw_same_sha_seen_skips_write(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    seen: set[str] = set()
    assert persist_raw(chunk(), store, seen) is True
    assert persist_raw(chunk(), store, seen) is False
    assert seen == {sha256_hex(b'{"a":1}')}


def test_gzip_is_deterministic(tmp_path: Path) -> None:
    persist_raw(chunk(), LocalStore(tmp_path / "a"))
    persist_raw(chunk(), LocalStore(tmp_path / "b"))
    assert (tmp_path / "a" / RAW).read_bytes() == (tmp_path / "b" / RAW).read_bytes()
