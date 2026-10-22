"""Source protocol, raw-chunk model and idempotent raw persistence (SPEC-01 §2-3, T-102).

Raw API responses are stored under ``raw/{source}/{series}/{yyyymmdd}.json.gz`` (built only by
:func:`tidal.storage.layout.raw_path`) before any normalisation, so ingestion can be audited and
replayed without hitting the upstream API again.
"""

import gzip
import hashlib
import json
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Final, Protocol

import numpy as np

from tidal.common.errors import NotFound
from tidal.contracts.instruments import CATALOGUE_V1
from tidal.storage import layout
from tidal.storage.parquet_writer import ImmutableFileExists, ObjectStore

CONTENT_TYPE_JSON: Final = "application/json"
MAX_ATTEMPTS: Final = 5
BACKOFF_BASE_S: Final = 1.0
MAX_BACKOFF_S: Final = 60.0
MAX_RETRY_AFTER_S: Final = 300.0
_KEY_RE: Final = re.compile(r"(api_key=)[^&\s'\"]+", re.IGNORECASE)


def redact_url(text: str) -> str:
    """Mask the ``api_key`` query parameter in ``text`` (URLs, messages)."""
    return _KEY_RE.sub(r"\g<1>**********", text)


def retry_delay(attempt: int, retry_after: str | None, rng: np.random.Generator) -> float:
    """Backoff capped at 60 s with seeded jitter; numeric ``Retry-After`` raises it (cap 300 s)."""
    delay = min(MAX_BACKOFF_S, BACKOFF_BASE_S * 2**attempt) * (0.5 + 0.5 * float(rng.random()))
    if retry_after is not None and retry_after.strip().isdigit():
        delay = min(max(delay, float(retry_after)), MAX_RETRY_AFTER_S)
    return float(delay)


def _drop_api_key(node: object) -> object:
    if isinstance(node, dict):
        return {k: _drop_api_key(v) for k, v in node.items() if str(k).lower() != "api_key"}
    if isinstance(node, list):
        return [_drop_api_key(v) for v in node]
    return node


def strip_api_key(body: object, *, only: str | None = None, keep: str | None = None) -> object:
    """Remove ``api_key`` fields (any case, any depth) from a parsed JSON response.

    ``only="request"``: touch just that top-level subtree (EIA echoes params in ``request``);
    a body without it is returned unchanged. ``keep="observations"``: clean everything else,
    top level included, but leave that subtree's data alone (FRED). Neither: clean everything.
    """
    if only is not None and keep is not None:
        raise ValueError("only and keep are mutually exclusive")
    if not isinstance(body, dict):
        return body
    if only is not None:
        return {**body, only: _drop_api_key(body[only])} if only in body else body
    return {
        k: v if k == keep else _drop_api_key(v)
        for k, v in body.items()
        if str(k).lower() != "api_key"
    }


def strip_credential_echo(
    payload: bytes, *, only: str | None = None, keep: str | None = None
) -> bytes:
    """Raw payload = verbatim response minus any credential echo.

    Bytes are returned untouched unless an ``api_key`` field is removed; then the document is
    re-serialised compactly (original key order, ``ensure_ascii=False``) so the sha256 and
    digest never depend on the key value. Unparseable bodies are returned as is. See
    :func:`strip_api_key` for ``only`` / ``keep``.
    """
    try:
        body = json.loads(payload)
    except ValueError:
        return payload
    cleaned = strip_api_key(body, only=only, keep=keep)
    if cleaned == body:
        return payload
    return json.dumps(cleaned, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
    return layout.raw_path(chunk.source.lower(), chunk.series, chunk.day, chunk.sha256[:8])


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
