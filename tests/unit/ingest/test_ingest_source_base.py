import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from tidal.common.errors import NotFound
from tidal.contracts.instruments import CATALOGUE_V1
from tidal.ingest.sources import eia, fred
from tidal.ingest.sources.base import (
    RawChunk,
    instrument_for,
    persist_raw,
    raw_blob_path,
    redact_url,
    series_for,
    sha256_hex,
    strip_api_key,
    strip_credential_echo,
)
from tidal.storage.parquet_writer import LocalStore


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
    raw = raw_blob_path(c)
    assert raw == f"raw/eia/rwtc/20200420-{c.sha256[:8]}.json.gz"
    assert persist_raw(c, store) is True
    assert persist_raw(c, store) is False  # path exists -> no new write
    assert gzip.decompress((tmp_path / raw).read_bytes()) == c.payload
    assert list(store.list("raw/eia/")) == [raw]


def test_persist_raw_same_sha_seen_skips_write(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    seen: set[str] = set()
    assert persist_raw(chunk(), store, seen) is True
    assert persist_raw(chunk(), store, seen) is False
    assert seen == {sha256_hex(b'{"a":1}')}


def test_gzip_is_deterministic(tmp_path: Path) -> None:
    persist_raw(chunk(), LocalStore(tmp_path / "a"))
    persist_raw(chunk(), LocalStore(tmp_path / "b"))
    raw = raw_blob_path(chunk())
    assert (tmp_path / "a" / raw).read_bytes() == (tmp_path / "b" / raw).read_bytes()


def test_different_payload_same_day_is_new_blob(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert persist_raw(chunk(b'{"v":1}'), store) is True
    assert persist_raw(chunk(b'{"v":2}'), store) is True
    assert persist_raw(chunk(b'{"v":2}'), store) is False
    assert len(list(store.list("raw/eia/"))) == 2


FIXTURES = Path(__file__).parents[2] / "fixtures"


def test_strip_scopes_eia_request_and_fred_observations() -> None:
    body = {"request": {"params": {"api_key": "k"}}, "response": {"api_key": "keep"}}
    assert strip_api_key(body, only="request") == {
        "request": {"params": {}},
        "response": {"api_key": "keep"},
    }
    fred_body = {"api_key": "k", "meta": [{"API_KEY": "k"}], "observations": [{"api_key": "d"}]}
    assert strip_api_key(fred_body, keep="observations") == {
        "meta": [{}],
        "observations": [{"api_key": "d"}],
    }
    assert strip_api_key({"x": 1}, only="request") == {"x": 1}


def test_strip_only_and_keep_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        strip_api_key({}, only="request", keep="observations")
    with pytest.raises(ValueError, match="mutually exclusive"):
        strip_credential_echo(b"{}", only="request", keep="observations")


def test_redact_url_shared_and_case_insensitive() -> None:
    assert vars(eia)["redact_url"] is vars(fred)["redact_url"] is redact_url
    assert redact_url("u?API_KEY=abc'") == "u?API_KEY=**********'"


def _echoed(name: str, put: str) -> bytes:
    body = json.loads((FIXTURES / name).read_bytes())
    if put == "eia":
        body["request"]["params"]["api_key"] = "SECRETKEY"
    else:
        body["api_key"] = "SECRETKEY"
    return json.dumps(body, indent=2).encode()


@pytest.mark.parametrize(
    ("fixture", "kind", "kwargs", "digest"),
    [
        (
            "eia/rwtc_page1.json",
            "eia",
            {"only": "request"},
            "5f2c85ec935c7d637d449f67556bfee7771b31211fad7a08f444703d1c193589",
        ),
        (
            "fred/dexuseu_page1.json",
            "fred",
            {"keep": "observations"},
            "3f716bb84c5d52944dd8822cadb9b53db3c42b2b17a237970b06c7821c958503",
        ),
    ],
)
def test_stripped_payload_digest_is_pinned(
    fixture: str, kind: str, kwargs: dict[str, str], digest: str
) -> None:
    """Pinned so stored raw blobs stay idempotent across refactors of the stripper."""
    out = strip_credential_echo(_echoed(fixture, kind), **kwargs)
    assert b"SECRETKEY" not in out
    assert sha256_hex(out) == digest
