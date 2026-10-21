"""``/v1/health/live`` and ``/v1/health/ready`` (no auth); ready never returns details."""

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tidal.api.errors import CORRELATION_HEADER, correlation_header_doc, problem_docs
from tidal.common.logging import get_logger
from tidal.contracts.models import HealthStatus

PING_TIMEOUT_S = 2.0

router = APIRouter(prefix="/v1/health", tags=["health"])
logger = get_logger(__name__)

_PUBLIC: dict[str, Any] = {"security": [], "x-tidal-min-role": "none"}  # contract: no auth


def _ok_doc(description: str, status: str, *, closed: bool) -> dict[str, Any]:
    """Contract response doc: ``{status: <value>}`` plus the correlation header."""
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string", "enum": [status]}},
    }
    if closed:
        schema["additionalProperties"] = False
    return {
        "description": description,
        "headers": {CORRELATION_HEADER: correlation_header_doc()},
        "content": {"application/json": {"schema": schema}},
    }


@router.get(
    "/live",
    operation_id="getHealthLive",
    summary="Liveness probe",
    response_model=None,
    openapi_extra=_PUBLIC,
    responses={200: _ok_doc("Process is alive.", "ok", closed=False), **problem_docs(429)},
)
async def live() -> dict[str, str]:
    """The process is alive."""
    return {"status": "ok"}


async def _check(name: str, client: Any) -> bool:
    """True when ``client.ping()`` succeeds; the failure reason is logged, never returned."""
    try:
        if client is None:
            raise LookupError("not configured")
        await asyncio.wait_for(client.ping(), PING_TIMEOUT_S)
    except Exception as exc:
        logger.warning("readiness_check_failed", dependency=name, error_type=type(exc).__name__)
        return False
    return True


@router.get(
    "/ready",
    operation_id="getHealthReady",
    summary="Readiness probe (SQL, Blob, Service Bus reachability)",
    response_model=None,
    openapi_extra=_PUBLIC,
    responses={
        200: _ok_doc("All dependencies reachable.", "ready", closed=True),
        **problem_docs(429),
        503: _ok_doc("Not ready.", "not_ready", closed=True),
    },
)
async def ready(request: Request) -> JSONResponse:
    """Ping the reader (SQL + Blob) and the bus; 200 ``ready`` or 503 ``not_ready``."""
    state = request.app.state
    checks = await asyncio.gather(_check("reader", state.reader), _check("bus", state.bus))
    ok = all(checks)
    body = HealthStatus(status="ready" if ok else "not_ready")
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)
