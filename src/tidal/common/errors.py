"""Typed error hierarchy (CLAUDE.md §7); the API maps these to RFC 7807 problem+json (T-110).

Each error carries an HTTP status and a problem ``type`` URI. ``detail`` is an author-written,
human-readable sentence: it is never derived from an exception message, SQL or a stack trace
(openapi ``Problem`` rule). Chain the cause with ``raise X(...) from exc`` so it reaches the logs
but never the response. No web-framework imports here.
"""

# Names follow the problem slugs (T-005), not the "...Error" suffix convention.
# ruff: noqa: N818

from typing import ClassVar

from tidal.contracts.models import Problem

PROBLEM_BASE_URI = "https://tidal.example/problems/"


class TidalError(Exception):
    """Base of all Tidal errors; unexpected failures map to 500 ``internal``."""

    status: ClassVar[int] = 500
    slug: ClassVar[str] = "internal"
    title: ClassVar[str] = "Internal error"

    def __init__(self, detail: str | None = None) -> None:
        """Create the error; ``detail`` must be safe to show to API clients."""
        super().__init__(detail or self.title)
        self.detail = detail

    @classmethod
    def type_uri(cls) -> str:
        """Problem ``type`` URI, ``https://tidal.example/problems/<slug>``."""
        return PROBLEM_BASE_URI + cls.slug

    def to_problem(self, correlation_id: str) -> Problem:
        """Return the contract ``Problem``; serialise with ``exclude_none=True``."""
        return Problem(
            type=self.type_uri(),
            title=self.title,
            status=self.status,
            detail=self.detail,
            correlation_id=correlation_id,
        )


class ValidationFailed(TidalError):
    """Request failed validation (400; FastAPI's 422 is mapped here)."""

    status = 400
    slug = "validation-failed"
    title = "Validation failed"


class Unauthenticated(TidalError):
    """Missing or invalid bearer token (401)."""

    status = 401
    slug = "unauthenticated"
    title = "Unauthenticated"


class Forbidden(TidalError):
    """Authenticated principal lacks the required role (403)."""

    status = 403
    slug = "forbidden"
    title = "Forbidden"


class NotFound(TidalError):
    """Unknown instrument or job, or a job owned by another principal (404, no existence leak)."""

    status = 404
    slug = "not-found"
    title = "Not found"


class RangeTooLarge(TidalError):
    """Requested range or sweep expansion exceeds the limits (413)."""

    status = 413
    slug = "range-too-large"
    title = "Range too large"


class RateLimited(TidalError):
    """Per-principal rate limit exceeded (429); ``retry_after`` feeds the ``Retry-After`` header."""

    status = 429
    slug = "rate-limited"
    title = "Too many requests"

    def __init__(self, retry_after: int, detail: str | None = None) -> None:
        """Create the error; ``retry_after`` is in whole seconds and must be ≥ 0."""
        if retry_after < 0:
            msg = "retry_after must be >= 0"
            raise ValueError(msg)
        super().__init__(detail)
        self.retry_after = retry_after


class DependencyUnavailable(TidalError):
    """A dependency (SQL, Blob, Service Bus, query service) is down (503)."""

    status = 503
    slug = "dependency-unavailable"
    title = "Dependency unavailable"


class ConfigError(TidalError):
    """Invalid configuration; raised at startup, never returned to a client in practice."""

    slug = "config-error"
    title = "Configuration error"
