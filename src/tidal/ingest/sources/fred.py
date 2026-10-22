"""FRED ``series/observations`` client (SPEC-01 §2-3, T-103).

REFERENCE CODE, UNWIRED since ADR-0013: no config key, CLI command or verify script uses it.

Mirrors :mod:`tidal.ingest.sources.eia`: one request per page (``limit``/``offset``, 100000 max),
retried with exponential backoff, seeded jitter and ``Retry-After``; each response is persisted raw
(injected store) *before* it is yielded, and the API key (a ``SecretStr`` in the query string) is
redacted from every log line, raw path and error message.

Endpoint, params and response shape are from documentation knowledge and UNVERIFIED against the
live API (no key in T-103; run ``scripts/verify_fred.py``)::

    GET https://api.stlouisfed.org/fred/series/observations
        ?series_id=DEXUSEU&api_key=..&file_type=json&observation_start=..&observation_end=..
        &limit=100000&offset=0
    -> {"realtime_start", "realtime_end", "count", "offset", "limit", ...,
        "observations": [{"realtime_start": .., "realtime_end": .., "date": "1999-01-04",
                          "value": "1.1812"}]}

The response does not carry ``series_id``, so :func:`parse_fred_page` takes it as an argument.
``realtime_start``/``realtime_end`` default to the request day, so the raw digest of an unchanged
page differs from day to day (one new blob per fetch day; content within a day is idempotent).
"""

import json
import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Final

import httpx
import numpy as np
import polars as pl
import pyarrow as pa
from pydantic import SecretStr

from tidal.common.errors import ConfigError, DependencyUnavailable
from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock, SystemClock
from tidal.ingest.sources.base import (
    CONTENT_TYPE_JSON,
    MAX_ATTEMPTS,
    RawChunk,
    persist_raw,
    redact_url,
    retry_delay,
    series_for,
    sha256_hex,
    strip_credential_echo,
)
from tidal.storage.parquet_writer import ObjectStore

BASE_URL = "https://api.stlouisfed.org/fred"
PAGE_LIMIT = 100_000
MAX_PAGES = 100
_HTTP_BAD_REQUEST = 400
_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500
#: FRED's marker for a missing observation (holidays etc.).
MISSING_VALUE: Final = "."

PAGE_SCHEMA: Final = pa.schema(
    [pa.field(n, t) for n, t in (("ts_ns", pa.int64()), ("value", pa.float64()))]
    + [pa.field("series", pa.string())]
)
_log = get_logger(__name__)


class FredRequestRejected(DependencyUnavailable):
    """FRED rejected the request with a non-retryable 4xx (bad key, bad series, bad query)."""

    slug = "fred-request-rejected"
    title = "FRED rejected the request"


class FredUnavailable(DependencyUnavailable):
    """FRED stayed unavailable after all retries, or the response was unusable."""

    slug = "fred-unavailable"
    title = "FRED unavailable"


class FredSource:
    """FRED ``Source``: ``fetch`` yields one persisted :class:`RawChunk` per page."""

    def __init__(
        self,
        client: httpx.Client,
        api_key: SecretStr | None,
        store: ObjectStore,
        rng: np.random.Generator,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
        max_pages: int = MAX_PAGES,
    ) -> None:
        """Wire injected collaborators; ``api_key`` is a plain argument (ADR-0013)."""
        if api_key is None or not api_key.get_secret_value():
            raise ConfigError("FRED api_key is empty")
        self._client = client
        self._key = api_key
        self._store = store
        self._rng = rng
        self._clock = clock or SystemClock()
        self._sleep = sleep
        self._base = base_url.rstrip("/")
        self._max_pages = max_pages

    def _get(self, url: str, params: dict[str, str | int]) -> bytes:
        """GET with retry on transport errors, 429 and 5xx; other 4xx fail fast."""
        full = {**params, "api_key": self._key.get_secret_value()}
        for attempt in range(MAX_ATTEMPTS):
            retry_after: str | None = None
            try:
                resp = self._client.get(url, params=full)
            except httpx.TransportError as exc:
                status = type(exc).__name__
            else:
                code = resp.status_code
                if code == httpx.codes.OK:
                    return resp.content
                if code < _HTTP_BAD_REQUEST:
                    raise FredUnavailable(f"FRED returned unexpected HTTP {code}")
                if code != _HTTP_TOO_MANY and code < _HTTP_SERVER_ERROR:
                    _log.warning(
                        "fred_request_rejected", url=redact_url(str(resp.request.url)), status=code
                    )
                    raise FredRequestRejected(f"FRED returned HTTP {code}")
                status = str(code)
                retry_after = resp.headers.get("Retry-After")
            if attempt == MAX_ATTEMPTS - 1:
                break
            delay = retry_delay(attempt, retry_after, self._rng)
            _log.warning(
                "fred_retry", url=redact_url(url), status=status, attempt=attempt + 1, delay_s=delay
            )
            self._sleep(delay)
        raise FredUnavailable(f"FRED still failing after {MAX_ATTEMPTS} attempts")

    def fetch(self, instrument_id: str, start: date, end: date) -> Iterator[RawChunk]:
        """Yield raw pages for ``[start, end]``, each persisted before it is yielded."""
        series = series_for(instrument_id)
        url = f"{self._base}/series/observations"
        seen: set[str] = set()
        offset = 0
        for page in range(self._max_pages):
            params: dict[str, str | int] = {
                "series_id": series,
                "file_type": "json",
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
                "sort_order": "asc",
                "limit": PAGE_LIMIT,
                "offset": offset,
            }
            payload = strip_credential_echo(self._get(url, params), keep="observations")
            if self._key.get_secret_value().encode() in payload:
                raise FredUnavailable("FRED response echoes the credential")
            fetched_at_ns = self._clock.now_ns()
            total, first_day, n_rows = _page_info(payload, start)
            chunk = RawChunk(
                "FRED",
                series,
                instrument_id,
                fetched_at_ns,
                payload,
                CONTENT_TYPE_JSON,
                page,
                sha256_hex(payload),
                first_day,
            )
            persist_raw(chunk, self._store, seen)
            _log.info("fred_page", series=series, page=page, rows=n_rows, total=total)
            yield chunk
            offset += n_rows
            if n_rows == 0 or offset >= total:
                return
        raise FredUnavailable(f"FRED pagination exceeded {self._max_pages} pages")


def _page_info(payload: bytes, fallback_day: date) -> tuple[int, date, int]:
    """Return ``(count, first observation date, row count)`` of an observations response."""
    try:
        body = json.loads(payload)
        obs = body["observations"]
        total = int(body["count"])
        first = date.fromisoformat(obs[0]["date"]) if obs else fallback_day
    except (ValueError, KeyError, TypeError, IndexError):
        raise FredUnavailable("FRED response is not a valid observations page") from None
    return total, first, len(obs)


def parse_fred_page(payload: bytes, series: str) -> pa.Table:
    """Parse an observations page to ``(ts_ns int64, value float64, series string)``.

    ``series`` is passed in because the response does not contain it. ``ts_ns`` is UTC midnight
    of the observation date (ADR-0009). FRED's missing marker ``"."`` (and null/non-numeric
    values) become NaN, never filled. Normalisation to BAR_V1 is T-105's job: it must drop (or
    flag) the NaN rows, because data-contracts §3.1 says OHLC is never NaN.
    """
    try:
        obs = json.loads(payload)["observations"]
    except (ValueError, KeyError, TypeError):
        raise FredUnavailable("FRED response is not a valid observations page") from None
    if not obs:
        return PAGE_SCHEMA.empty_table()
    try:
        df = pl.from_dicts(obs, infer_schema_length=None).select(
            pl.col("date")
            .str.to_date("%Y-%m-%d")
            .cast(pl.Datetime("ns"))
            .cast(pl.Int64)
            .alias("ts_ns"),
            pl.col("value").cast(pl.String).cast(pl.Float64, strict=False).fill_null(float("nan")),
            pl.lit(series).alias("series"),
        )
    except pl.exceptions.PolarsError:
        raise FredUnavailable("FRED response is not a valid observations page") from None
    return df.to_arrow().cast(PAGE_SCHEMA)
