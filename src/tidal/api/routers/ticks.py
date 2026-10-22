"""``GET /v1/ticks`` (Trader+, max 31 days per call)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from tidal.api.auth_roles import TRADER, TraderPrincipal
from tidal.api.deps import get_reader
from tidal.api.errors import problem_docs
from tidal.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    binding,
    cursor_key,
    decode_cursor,
    encode_cursor,
    page_of,
)
from tidal.api.routers.bars import (
    DAY_NS,
    Format,
    TimeParam,
    market_data_ok,
    parse_range,
)
from tidal.api.streaming import TIER_HEADER, stream_response
from tidal.common.errors import RangeTooLarge
from tidal.contracts.instruments import InstrumentId
from tidal.contracts.models import TicksResponse
from tidal.contracts.schemas import TICK_V1
from tidal.query.protocols import Reader

TICKS_MAX_NS = 31 * DAY_NS  # SPEC-04 §3

router = APIRouter(prefix="/v1", tags=["market-data"])


@router.get(
    "/ticks",
    operation_id="getTicks",
    summary="Ticks for one instrument (max 31 days per call)",
    response_model=TicksResponse,
    openapi_extra={"x-tidal-min-role": TRADER},
    responses={200: market_data_ok(), **problem_docs(400, 401, 403, 404, 413, 429, 503)},
)
async def get_ticks(  # noqa: PLR0917 - contract query parameters
    request: Request,
    response: Response,
    principal: TraderPrincipal,
    reader: Annotated[Reader, Depends(get_reader)],
    instrument_id: Annotated[InstrumentId, Query()],
    start: TimeParam,
    end: TimeParam,
    format: Format = Format.JSON,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> TicksResponse | StreamingResponse:
    """Ticks in ``[start, end)``; arrow/parquet stream the range, JSON is cursor-paginated."""
    start_ns, end_ns = parse_range(start, end)
    if end_ns - start_ns > TICKS_MAX_NS:
        raise RangeTooLarge("Ticks are limited to 31 days per call.")
    if format is not Format.JSON:
        stream = await reader.stream_ticks(instrument_id, start_ns, end_ns)
        return stream_response(stream, TICK_V1, arrow=format is Format.ARROW)
    bound = binding("ticks", principal.oid, instrument_id, start_ns, end_ns)
    key = cursor_key(request)
    pos = decode_cursor(key, bound, cursor) if cursor else None
    got = await reader.read_ticks(instrument_id, pos.ts_ns if pos else start_ns, end_ns)
    page, nxt = page_of(got.rows, lambda t: t.ts_ns, pos, limit)
    response.headers[TIER_HEADER] = got.tier.value
    return TicksResponse(
        instrument_id=instrument_id,
        tier=got.tier,
        rows=page,
        next_cursor=encode_cursor(key, bound, nxt) if nxt else None,
    )
