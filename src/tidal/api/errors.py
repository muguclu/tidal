"""Exception handlers mapping errors to RFC 7807 ``application/problem+json`` (api-contract §4).

``detail`` is never derived from an exception message or from client input values.
"""

import re
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from tidal.common.errors import (
    PROBLEM_BASE_URI,
    NotFound,
    RateLimited,
    TidalError,
    ValidationFailed,
)
from tidal.common.logging import get_correlation_id, get_logger, new_correlation_id
from tidal.contracts.models import Problem

PROBLEM_MEDIA_TYPE = "application/problem+json"
CORRELATION_HEADER = "X-Correlation-Id"
_MAX_REPORTED_ERRORS = 5
_SAFE_LOC = re.compile(r"[^A-Za-z0-9_.\[\]-]")

logger = get_logger(__name__)

_DESCRIPTIONS = {
    400: "Validation error (400).",
    401: "Missing or invalid token (401).",
    403: "Required role missing (403).",
    404: "Unknown instrument or job (404).",
    413: "Requested range or sweep too large (413).",
    429: "Rate limited (429), 60 requests per minute per principal.",
    503: "A dependency is down (503).",
}


def correlation_header_doc() -> dict[str, Any]:
    """OpenAPI ``X-Correlation-Id`` response header (contract ``components.headers``)."""
    return {
        "description": "Request correlation id; echoed if supplied, else generated.",
        "required": True,
        "schema": {"type": "string"},
    }


def problem_docs(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries for problem+json statuses (400/401/403/404/413/429/503).

    ``model`` makes FastAPI emit the ``Problem`` schema; ``rename_problem_media_types`` then
    moves it from ``application/json`` to ``application/problem+json``.
    """
    docs: dict[int | str, dict[str, Any]] = {}
    for status in statuses:
        headers: dict[str, Any] = {CORRELATION_HEADER: correlation_header_doc()}
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            headers["Retry-After"] = {
                "description": "Seconds to wait before retrying.",
                "required": True,
                "schema": {"type": "integer", "minimum": 1},
            }
        docs[status] = {"model": Problem, "description": _DESCRIPTIONS[status], "headers": headers}
    return docs


def rename_problem_media_types(schema: dict[str, Any]) -> dict[str, Any]:
    """Re-key every ``Problem`` response body from ``application/json`` to problem+json."""
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            for response in operation.get("responses", {}).values():
                content = response.get("content", {})
                body = content.get("application/json")
                if body == {"schema": {"$ref": "#/components/schemas/Problem"}}:
                    del content["application/json"]
                    content[PROBLEM_MEDIA_TYPE] = body
    return schema


def _correlation_id() -> str:
    return get_correlation_id() or new_correlation_id()


def problem_response(problem: Problem, headers: dict[str, str] | None = None) -> JSONResponse:
    """Serialise ``problem`` (``exclude_none``) as problem+json with the correlation header."""
    return JSONResponse(
        problem.model_dump(mode="json", exclude_none=True),
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers={CORRELATION_HEADER: problem.correlation_id, **(headers or {})},
    )


def internal_error_response() -> JSONResponse:
    """Generic 500 problem; the caller logs the exception, it is never returned."""
    problem = TidalError("An unexpected error occurred.").to_problem(_correlation_id())
    return problem_response(problem)


async def _tidal_error(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, TidalError)  # noqa: S101 - registered for TidalError only
    headers = None
    if isinstance(exc, RateLimited):
        headers = {"Retry-After": str(max(exc.retry_after, 1))}  # openapi: integer >= 1
    if exc.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        logger.error("request_failed", error_type=type(exc).__name__, exc_info=exc)
    return problem_response(exc.to_problem(_correlation_id()), headers)


async def _validation_error(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for it only
    # Report where the problem is, never the offending value; names are sanitised and capped.
    where = [
        _SAFE_LOC.sub("", ".".join(str(p) for p in err["loc"]))[:64]
        for err in exc.errors()[:_MAX_REPORTED_ERRORS]
    ]
    detail = "Invalid request parameters: " + ", ".join(where) + "."
    return problem_response(ValidationFailed(detail).to_problem(_correlation_id()))


async def _http_error(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for it only
    if exc.status_code == HTTPStatus.NOT_FOUND:
        return problem_response(NotFound("Resource not found.").to_problem(_correlation_id()))
    problem = Problem(
        type=PROBLEM_BASE_URI + "http-error",
        title=HTTPStatus(exc.status_code).phrase,
        status=exc.status_code,
        correlation_id=_correlation_id(),
    )
    return problem_response(problem, dict(exc.headers or {}))


def register_exception_handlers(app: FastAPI) -> None:
    """Install the problem+json handlers; unhandled errors are caught by the ASGI middleware."""
    app.add_exception_handler(TidalError, _tidal_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
