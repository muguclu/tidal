"""Interfaces the API depends on; the real reader (T-208) and Service Bus client implement them.

Tests and Phase-1 routers use the in-memory fakes in ``tests/fakes`` (SPEC-04 §3). Time bounds are
int64 epoch nanoseconds (``ts_ns``); ``start`` is inclusive and ``end`` exclusive.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class Clock(Protocol):
    """Source of the current time; tests inject a frozen clock."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""
        ...


@runtime_checkable
class Reader(Protocol):
    """Unified read access over the hot (SQL) and cold (Blob) stores."""

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

    async def latest(self, instrument_ids: Sequence[str]) -> list[LatestBar]:
        """Last bar of each instrument (hot store only); unknown ids are skipped."""
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
