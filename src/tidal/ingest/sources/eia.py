"""EIA Open Data API v2 client (SPEC-01 §2-3, T-102).

One request per page (``offset``/``length``, 5000 max), retried with exponential backoff, jitter
from an injected numpy ``Generator`` and ``Retry-After``. Each response is persisted raw (via the
injected store) *before* it is yielded, so :func:`parse_eia_page` and T-105 normalisation always
run on stored bytes. The API key is a ``SecretStr``; it travels in the query string (EIA v2
design) but is redacted from every log line, raw path and error message.

Routes (EIA API browser; UNVERIFIED against the live API, no key available in T-102):
``petroleum/pri/spt`` RBRTE, RWTC; ``petroleum/pri/fut`` RCLC1-4; ``natural-gas/pri/fut``
RNGWHHD, RNGC1-4. The series facet is ``series``; values arrive as JSON strings.
"""

import json
import re
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

BASE_URL = "https://api.eia.gov/v2"
PAGE_LENGTH = 5000
MAX_PAGES = 500
_HTTP_BAD_REQUEST = 400
_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500

ROUTES: dict[str, str] = {
    "RBRTE": "petroleum/pri/spt",
    "RWTC": "petroleum/pri/spt",
    **{f"RCLC{n}": "petroleum/pri/fut" for n in range(1, 5)},
    "RNGWHHD": "natural-gas/pri/fut",
    **{f"RNGC{n}": "natural-gas/pri/fut" for n in range(1, 5)},
}

PAGE_SCHEMA: Final = pa.schema(
    [pa.field(n, t) for n, t in (("ts_ns", pa.int64()), ("value", pa.float64()))]
    + [pa.field("series", pa.string())]
)
#: EIA stopped publishing NYMEX futures (RCLC1-4, RNGC1-4) after this date (EIA API dashboard,
#: ``petroleum/pri/fut``); FUT1-4 instruments are backfill-only and requests are clipped here.
EIA_FUTURES_LAST_DATE: Final = date(2024, 4, 5)
_FUTURES_SERIES = re.compile(r"R(CL|NG)C\d")
_log = get_logger(__name__)


class EiaRequestRejected(DependencyUnavailable):
    """EIA rejected the request with a non-retryable 4xx (bad key, forbidden, bad query)."""

    slug = "eia-request-rejected"
    title = "EIA rejected the request"


class EiaUnavailable(DependencyUnavailable):
    """EIA stayed unavailable after all retries, or the response was unusable."""

    slug = "eia-unavailable"
    title = "EIA unavailable"


class EiaSource:
    """EIA v2 ``Source``: ``fetch`` yields one persisted :class:`RawChunk` per page."""

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
        """Wire injected collaborators; ``api_key`` comes from ``settings.sources.eia_api_key``."""
        if api_key is None or not api_key.get_secret_value():
            raise ConfigError("TIDAL_SOURCES__EIA_API_KEY is not set")
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
                if code < _HTTP_BAD_REQUEST:
                    return resp.content
                if code != _HTTP_TOO_MANY and code < _HTTP_SERVER_ERROR:
                    _log.warning(
                        "eia_request_rejected", url=redact_url(str(resp.request.url)), status=code
                    )
                    raise EiaRequestRejected(f"EIA returned HTTP {code}")
                status = str(code)
                retry_after = resp.headers.get("Retry-After")
            if attempt == MAX_ATTEMPTS - 1:
                break
            delay = retry_delay(attempt, retry_after, self._rng)
            _log.warning(
                "eia_retry", url=redact_url(url), status=status, attempt=attempt + 1, delay_s=delay
            )
            self._sleep(delay)
        raise EiaUnavailable(f"EIA still failing after {MAX_ATTEMPTS} attempts")

    def fetch(self, instrument_id: str, start: date, end: date) -> Iterator[RawChunk]:
        """Yield raw pages for ``[start, end]``, each persisted before it is yielded."""
        series = series_for(instrument_id)
        route = ROUTES.get(series)
        if route is None:
            raise EiaRequestRejected(f"no EIA route for series {series}")
        if _FUTURES_SERIES.fullmatch(series):
            end = min(end, EIA_FUTURES_LAST_DATE)
            if start > end:
                return
        url = f"{self._base}/{route}/data/"
        seen: set[str] = set()
        offset = 0
        for page in range(self._max_pages):
            params: dict[str, str | int] = {
                "frequency": "daily",
                "data[0]": "value",
                "facets[series][]": series,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
                "offset": offset,
                "length": PAGE_LENGTH,
            }
            payload = strip_credential_echo(self._get(url, params), only="request")
            if self._key.get_secret_value().encode() in payload:
                raise EiaUnavailable("EIA response echoes the credential outside request.params")
            fetched_at_ns = self._clock.now_ns()
            total, first_day, n_rows = _page_info(payload, start)
            chunk = RawChunk(
                "EIA",
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
            _log.info("eia_page", series=series, page=page, rows=n_rows, total=total)
            yield chunk
            offset += n_rows
            if n_rows == 0 or offset >= total:
                return
        raise EiaUnavailable(f"EIA pagination exceeded {self._max_pages} pages")


def _page_info(payload: bytes, fallback_day: date) -> tuple[int, date, int]:
    """Return ``(total, first period date, row count)`` of a v2 response."""
    try:
        resp = json.loads(payload)["response"]
        data = resp["data"]
        total = int(resp["total"])
        first = date.fromisoformat(data[0]["period"]) if data else fallback_day
    except (ValueError, KeyError, TypeError, IndexError):
        raise EiaUnavailable("EIA response is not a valid v2 data page") from None
    return total, first, len(data)


def parse_eia_page(payload: bytes) -> pa.Table:
    """Parse a v2 page to ``(ts_ns int64, value float64, series string)``.

    ``ts_ns`` is UTC midnight of the trade date (ADR-0009). Negative values are kept; null or
    non-numeric values become NaN (never filled). Normalisation to BAR_V1 is T-105's job: it must
    drop (or flag) the NaN rows, because data-contracts §3.1 says OHLC is never NaN.
    """
    try:
        data = json.loads(payload)["response"]["data"]
    except (ValueError, KeyError, TypeError):
        raise EiaUnavailable("EIA response is not a valid v2 data page") from None
    schema = PAGE_SCHEMA
    if not data:
        return schema.empty_table()
    df = pl.from_dicts(data, infer_schema_length=None).select(
        pl.col("period")
        .str.to_date("%Y-%m-%d")
        .cast(pl.Datetime("ns"))
        .cast(pl.Int64)
        .alias("ts_ns"),
        pl.col("value").cast(pl.String).cast(pl.Float64, strict=False).fill_null(float("nan")),
        pl.col("series").cast(pl.String),
    )
    return df.to_arrow().cast(schema)
