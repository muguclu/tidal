"""``GET /v1/spreads``: leg_a close minus leg_b close, inner-joined on ``ts_ns`` (Trader+)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from tidal.api.auth_roles import TRADER, TraderPrincipal
from tidal.api.deps import get_reader
from tidal.api.errors import problem_docs
from tidal.api.routers.bars import (
    TIER_HEADER,
    TimeParam,
    check_bars_range,
    market_data_ok,
    parse_range,
    tier_of,
)
from tidal.common.errors import ValidationFailed
from tidal.contracts.instruments import InstrumentId
from tidal.contracts.models import SpreadPoint, SpreadResponse
from tidal.contracts.schemas import Interval
from tidal.query.protocols import Reader

router = APIRouter(prefix="/v1", tags=["market-data"])


@router.get(
    "/spreads",
    operation_id="getSpreads",
    summary="Spread series leg_a minus leg_b",
    description="Timestamps present in both legs only (inner join); no forward-fill.",
    response_model=SpreadResponse,
    openapi_extra={"x-tidal-min-role": TRADER},
    responses={
        200: market_data_ok(binary=False),
        **problem_docs(400, 401, 403, 404, 413, 429, 503),
    },
)
async def get_spreads(  # noqa: PLR0917 - contract query parameters
    response: Response,
    _principal: TraderPrincipal,
    reader: Annotated[Reader, Depends(get_reader)],
    leg_a: Annotated[InstrumentId, Query()],
    leg_b: Annotated[InstrumentId, Query()],
    interval: Annotated[Interval, Query()],
    start: TimeParam,
    end: TimeParam,
) -> SpreadResponse:
    """Inner join of the two legs on ``ts_ns``; exactly two distinct legs."""
    if leg_a == leg_b:
        raise ValidationFailed("A spread needs two different instruments.")
    start_ns, end_ns = parse_range(start, end)
    check_bars_range(interval, start_ns, end_ns)
    a = await reader.read_bars(leg_a, interval, start_ns, end_ns)
    b = await reader.read_bars(leg_b, interval, start_ns, end_ns)
    by_ts = {bar.ts_ns: bar for bar in b.rows}
    rows = [
        SpreadPoint(
            ts=bar.ts,
            ts_ns=bar.ts_ns,
            leg_a=bar.close,
            leg_b=other.close,
            spread=bar.close - other.close,
        )
        for bar in a.rows
        if (other := by_ts.get(bar.ts_ns)) is not None
    ]
    tier = tier_of([a.tier, b.tier])
    response.headers[TIER_HEADER] = tier.value
    return SpreadResponse(leg_a=leg_a, leg_b=leg_b, interval=interval, tier=tier, rows=rows)
