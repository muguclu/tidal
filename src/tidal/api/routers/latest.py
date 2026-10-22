"""``GET /v1/latest``: last bar per instrument, hot store only."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from tidal.api.auth_roles import READER, ReaderPrincipal
from tidal.api.deps import get_reader
from tidal.api.errors import problem_docs
from tidal.api.routers.bars import market_data_ok
from tidal.api.streaming import TIER_HEADER
from tidal.contracts.instruments import InstrumentId
from tidal.contracts.models import LatestResponse, Tier
from tidal.contracts.schemas import Interval
from tidal.query.protocols import Reader


def _all_of_default(schema: dict[str, Any]) -> None:
    """Match the contract's ``allOf`` + default form of the interval parameter."""
    ref = schema.pop("$ref")
    schema.clear()
    schema.update({"allOf": [{"$ref": ref}], "default": "1d"})


router = APIRouter(prefix="/v1", tags=["market-data"])


@router.get(
    "/latest",
    operation_id="getLatest",
    summary="Last bar per instrument (hot store only)",
    response_model=LatestResponse,
    openapi_extra={"x-tidal-min-role": READER},
    responses={
        200: market_data_ok(binary=False),
        **problem_docs(400, 401, 403, 404, 429, 503),
    },
)
async def get_latest(
    response: Response,
    _principal: ReaderPrincipal,
    reader: Annotated[Reader, Depends(get_reader)],
    instrument_id: Annotated[list[InstrumentId], Query(min_length=1, max_length=100)],
    interval: Annotated[Interval, Query(json_schema_extra=_all_of_default)] = Interval.D1,
) -> LatestResponse:
    """Latest bar of each requested instrument; instruments without data are omitted."""
    rows = await reader.latest(instrument_id, interval)
    response.headers[TIER_HEADER] = Tier.HOT.value
    return LatestResponse(tier=Tier.HOT, rows=rows)
