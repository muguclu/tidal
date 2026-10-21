"""Role checks (api-contract §1.1) and the per-principal rate limit as one dependency.

Reader < Trader < Quant are cumulative; Admin is separate and grants only ``/v1/admin/*``.
"""

from typing import Annotated, Any

from fastapi import Depends, Request

from tidal.api.deps import Principal, get_clock, get_principal
from tidal.api.ratelimit import TokenBucketLimiter
from tidal.common.errors import Forbidden
from tidal.query.protocols import Clock

READER, TRADER, QUANT, ADMIN = "Tidal.Reader", "Tidal.Trader", "Tidal.Quant", "Tidal.Admin"
_CHAIN = (READER, TRADER, QUANT)


def has_role(principal: Principal, minimum: str) -> bool:
    """True if ``principal`` holds ``minimum`` or or a higher role in the chain."""
    if minimum not in _CHAIN:
        return minimum in principal.roles
    return any(r in principal.roles for r in _CHAIN[_CHAIN.index(minimum) :])


def require_role(minimum: str) -> Any:  # Any: FastAPI's Depends() is itself typed Any
    """Dependency: authenticate, rate-limit the principal, then require ``minimum`` (else 403)."""

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(get_principal)],
        clock: Annotated[Clock, Depends(get_clock)],
    ) -> Principal:
        state = request.app.state
        if getattr(state, "rate_limiter", None) is None:
            state.rate_limiter = TokenBucketLimiter(clock)
        state.rate_limiter.check(principal.oid)
        if not has_role(principal, minimum):
            raise Forbidden("A higher role is required for this resource.")
        return principal

    return Depends(dependency)


ReaderPrincipal = Annotated[Principal, require_role(READER)]
TraderPrincipal = Annotated[Principal, require_role(TRADER)]
