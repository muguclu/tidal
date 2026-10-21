"""``GET /v1/instruments`` and ``GET /v1/instruments/{id}`` (Reader+)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from tidal.api.auth_roles import READER, ReaderPrincipal
from tidal.api.deps import get_reader
from tidal.api.errors import problem_docs
from tidal.api.routers.bars import market_data_ok
from tidal.common.errors import NotFound
from tidal.contracts.instruments import AssetClass, Instrument, InstrumentId, Source
from tidal.contracts.models import InstrumentList
from tidal.query.protocols import Reader

router = APIRouter(prefix="/v1/instruments", tags=["reference"])


@router.get(
    "",
    operation_id="listInstruments",
    summary="List instruments",
    response_model=InstrumentList,
    openapi_extra={"x-tidal-min-role": READER},
    responses={
        200: market_data_ok(binary=False, tier=False),
        **problem_docs(400, 401, 403, 429, 503),
    },
)
async def list_instruments(
    _principal: ReaderPrincipal,
    reader: Annotated[Reader, Depends(get_reader)],
    asset_class: AssetClass | None = None,
    source: Source | None = None,
) -> InstrumentList:
    """Instruments matching the filters (all if none given)."""
    return InstrumentList(items=await reader.instruments(asset_class=asset_class, source=source))


@router.get(
    "/{id}",
    operation_id="getInstrument",
    summary="Get one instrument",
    response_model=Instrument,
    openapi_extra={"x-tidal-min-role": READER},
    responses={
        200: market_data_ok(binary=False, tier=False),
        **problem_docs(400, 401, 403, 404, 429, 503),
    },
)
async def get_instrument(
    id: Annotated[InstrumentId, Path()],
    _principal: ReaderPrincipal,
    reader: Annotated[Reader, Depends(get_reader)],
) -> Instrument:
    """One instrument by id; 404 when unknown."""
    for instrument in await reader.instruments():
        if instrument.instrument_id == id:
            return instrument
    raise NotFound("Unknown instrument.")
