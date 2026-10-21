"""Source protocol, raw-chunk model and idempotent raw persistence (SPEC-01 §2-3, T-102).

Raw API responses are stored under ``raw/{source}/{series}/{yyyymmdd}.json.gz`` (built only by
:func:`tidal.storage.layout.raw_path`) before any normalisation, so ingestion can be audited and
replayed without hitting the upstream API again.
"""

import gzip
import hashlib
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Final, Protocol

from tidal.common.errors import NotFound
from tidal.contracts.instruments import CATALOGUE_V1
from tidal.storage import layout
from tidal.storage.parquet_writer import ImmutableFileExists, ObjectStore

CONTENT_TYPE_JSON: Final = "application/json"


@dataclass(frozen=True, slots=True)
class RawChunk:
    """One raw upstream response page, exactly as received.

    ``day`` is the date used for the raw blob name (first observation date of the page, so
    pages of one fetch never collide); ``sha256`` is the hex digest of ``payload``.
    """

    source: str
    series: str
    instrument_id: str
    fetched_at_ns: int
    payload: bytes
    content_type: str
    page: int
    sha256: str
    day: date


class Source(Protocol):
    """A market-data source yielding raw response pages (one chunk per page)."""

    def fetch(self, instrument_id: str, start: date, end: date) -> Iterator[RawChunk]:
        """Yield raw chunks for ``instrument_id`` over the inclusive range ``[start, end]``."""
        ...


def sha256_hex(payload: bytes) -> str:
    """Hex SHA-256 of ``payload``."""
    return hashlib.sha256(payload).hexdigest()


@cache
def _series_map(prefix: str) -> dict[str, str]:
    return {
        e.instrument_id: e.source_series
        for e in CATALOGUE_V1
        if e.instrument_id.startswith(prefix + ".")
    }


def series_for(instrument_id: str) -> str:
    """Source series of a catalogued ``instrument_id`` (e.g. ``EIA.WTI.SPOT`` -> ``RWTC``)."""
    series = _series_map(instrument_id.split(".", 1)[0]).get(instrument_id)
    if series is None:
        raise NotFound(f"instrument {instrument_id!r} has no source series in the catalogue")
    return series


def instrument_for(source: str, series: str) -> str:
    """Inverse of :func:`series_for`: catalogued ``instrument_id`` of a source series."""
    for iid, s in _series_map(source.upper()).items():
        if s == series:
            return iid
    raise NotFound(f"series {series!r} of {source!r} is not in the catalogue")


def raw_blob_path(chunk: RawChunk) -> str:
    """Container-qualified raw path of ``chunk`` (via the layout module only)."""
    return layout.raw_path(chunk.source.lower(), chunk.series, chunk.day)


def persist_raw(chunk: RawChunk, store: ObjectStore, seen: set[str] | None = None) -> bool:
    """Gzip ``chunk.payload`` (deterministic, mtime 0) and store it create-only.

    Returns ``True`` if a blob was written and ``False`` if the content was already stored:
    either the sha was seen in this run (``seen``) or the path exists. The store has no read
    API, so an existing path with *different* content is indistinguishable and is not
    overwritten (raw blobs are immutable; see T-102 open questions).
    """
    if seen is not None:
        if chunk.sha256 in seen:
            return False
        seen.add(chunk.sha256)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "raw.json.gz"
        local.write_bytes(gzip.compress(chunk.payload, mtime=0))
        try:
            store.put_new(raw_blob_path(chunk), local)
        except ImmutableFileExists:
            return False
    return True
