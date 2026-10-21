"""In-memory ``Reader`` fake seeded with a few instruments and daily bars (SPEC-04 §3)."""

from collections.abc import Sequence
from datetime import UTC, datetime

from tidal.contracts.instruments import CATALOGUE_V1, AssetClass, Instrument, Source
from tidal.contracts.models import Bar, LatestBar, Tick, Tier
from tidal.contracts.schemas import Interval
from tidal.query.protocols import Rows

DAY_NS = 86_400 * 1_000_000_000
WTI_NEGATIVE_DAY = datetime(2020, 4, 20, tzinfo=UTC)


def _day(d: int) -> datetime:
    return datetime(2020, 4, d, tzinfo=UTC)


SEEDED_IDS = ("EIA.BRENT.SPOT", "EIA.WTI.SPOT", "EIA.HH.SPOT")


def make_bar(day: datetime, price: float) -> Bar:
    """A flat daily bar at ``price`` (negative prices are valid) with no volume."""
    ts_ns = int(day.timestamp()) * 1_000_000_000
    return Bar(
        ts=day, ts_ns=ts_ns, open=price, high=price, low=price, close=price,
        volume=None, quality=1, version=1,
    )  # fmt: skip


class FakeReader:
    """Reader over seeded data; ``fail_ping`` makes ``ping`` raise (readiness tests)."""

    def __init__(self, *, fail_ping: bool = False) -> None:
        """Seed three instruments with bars, including WTI -37.63 on 2020-04-20."""
        self.fail_ping = fail_ping
        self.instrument_rows = [
            e.to_instrument() for e in CATALOGUE_V1 if e.instrument_id in SEEDED_IDS
        ]
        self.bars: dict[str, list[Bar]] = {
            "EIA.BRENT.SPOT": [make_bar(_day(d), 25.0 + d) for d in (17, 20, 21)],
            "EIA.WTI.SPOT": [
                make_bar(_day(17), 18.27),
                make_bar(WTI_NEGATIVE_DAY, -37.63),
                make_bar(_day(21), 8.91),
            ],
            "EIA.HH.SPOT": [make_bar(_day(d), 1.6 + d / 100) for d in (17, 20, 21)],
        }

    async def read_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        *,
        as_of_ns: int | None = None,
    ) -> Rows[Bar]:
        """Daily bars in ``[start_ns, end_ns)``; other intervals are empty."""
        if interval is not Interval.D1:
            return Rows([], Tier.COLD)
        bars = [b for b in self.bars.get(instrument_id, []) if start_ns <= b.ts_ns < end_ns]
        return Rows(bars, Tier.COLD)

    async def read_ticks(
        self,
        instrument_id: str,
        start_ns: int,
        end_ns: int,
    ) -> Rows[Tick]:
        """No ticks are seeded."""
        return Rows([], Tier.HOT)

    async def latest(self, instrument_ids: Sequence[str]) -> list[LatestBar]:
        """Last seeded bar of each known instrument."""
        return [
            LatestBar(instrument_id=i, interval=Interval.D1, bar=self.bars[i][-1])
            for i in instrument_ids
            if self.bars.get(i)
        ]

    async def instruments(
        self, *, asset_class: AssetClass | None = None, source: Source | None = None
    ) -> list[Instrument]:
        """Seeded instruments, filtered."""
        return [
            i
            for i in self.instrument_rows
            if (asset_class is None or i.asset_class is asset_class)
            and (source is None or i.source is source)
        ]

    async def ping(self) -> None:
        """Raise ``ConnectionError`` when ``fail_ping`` is set."""
        if self.fail_ping:
            raise ConnectionError("fake store down")
