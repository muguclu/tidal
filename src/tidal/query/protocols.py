"""Interfaces the API depends on; the real reader (T-208) and Service Bus client implement them.

Tests and Phase-1 routers use the in-memory fakes in ``tests/fakes`` (SPEC-04 §3). Time bounds are
int64 epoch nanoseconds (``ts_ns``); ``start`` is inclusive and ``end`` exclusive.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import pyarrow as pa

from tidal.contracts.instruments import AssetClass, Instrument, Source
from tidal.contracts.messages import BacktestJobMessage, TieringJobMessage
from tidal.contracts.models import Bar, LatestBar, Tick, Tier
from tidal.contracts.schemas import Interval

#: Messages the API may enqueue (messaging-contract).
OutboundMessage = BacktestJobMessage | TieringJobMessage


@dataclass(frozen=True, slots=True)
class Rows[T]:
    """Rows of one read plus the tier that served them (``X-Tidal-Tier``)."""

    rows: list[T]
    tier: Tier


@dataclass(frozen=True, slots=True)
class RowStream:
    """Streamed read: ``tier`` is known before the first batch; rows are never all materialised.

    ``batches`` yields record batches in the contract arrow schema (``BAR_V1`` / ``TICK_V1``).
    """

    tier: Tier
    batches: AsyncIterator[pa.RecordBatch]


@dataclass(frozen=True, slots=True)
class ObservedRowStream(RowStream):
    """A ``RowStream`` whose tier is refined while it is consumed.

    ``tier`` stays the up-front label of the requested range (arrow/parquet headers are sent
    before any data). ``observed()`` is the tier of what has actually been read so far: ``mixed``
    as soon as a slice held rows on both sides of the watermark (late restatement in SQL, rows
    not yet deleted after migration). JSON paging reads it after the page, when it is final.
    """

    observed: Callable[[], Tier]


def observed_tier(stream: RowStream) -> Tier:
    """Tier of the rows read from ``stream`` so far (its up-front label if it cannot refine)."""
    return stream.observed() if isinstance(stream, ObservedRowStream) else stream.tier


@runtime_checkable
class Clock(Protocol):
    """Source of the current time; tests inject a frozen clock."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""
        ...


@runtime_checkable
class Reader(Protocol):
    """Unified read access over the hot (SQL) and cold (Blob) stores.

    ``read_*`` and ``stream_*`` raise ``NotFound`` for an unknown ``instrument_id``; they never
    return empty rows for it (a known instrument with no data in range yields no rows).

    Rows with equal ``ts_ns`` must come back in a stable order (key order: instrument, ts_ns,
    side, version), because the API's keyset pagination resumes by position within a timestamp.
    """

    async def read_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        *,
        as_of_ns: int | None = None,
    ) -> Rows[Bar]:
        """Bars in ``[start_ns, end_ns)``; ``as_of_ns`` selects the version known at that time."""
        ...

    async def read_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> Rows[Tick]:
        """Ticks in ``[start_ns, end_ns)``."""
        ...

    async def stream_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        *,
        as_of_ns: int | None = None,
    ) -> RowStream:
        """Like ``read_bars`` for arrow/parquet output, as ``BAR_V1`` batches."""
        ...

    async def stream_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> RowStream:
        """Like ``read_ticks`` for arrow/parquet output, as ``TICK_V1`` batches."""
        ...

    async def latest(
        self, instrument_ids: Sequence[str], interval: Interval = Interval.D1
    ) -> list[LatestBar]:
        """Last ``interval`` bar of each instrument (hot store only); unknown ids are skipped."""
        ...

    async def instruments(
        self, *, asset_class: AssetClass | None = None, source: Source | None = None
    ) -> list[Instrument]:
        """Reference data, optionally filtered."""
        ...

    async def ping(self) -> None:
        """Check SQL and Blob reachability; raise any exception when a store is down."""
        ...


@runtime_checkable
class Bus(Protocol):
    """Service Bus producer."""

    async def send_batch(self, messages: Sequence[OutboundMessage]) -> int:
        """Send ``messages`` in service-bus batches and return how many were sent."""
        ...

    async def ping(self) -> None:
        """Check Service Bus reachability; raise any exception when it is down."""
        ...
