"""``EntraTokenValidator``: tidal-query accepts only the id-api workload identity (T-402)."""

from collections.abc import Callable

import httpx
import pytest

from fakes.jwks.core import TEST_TENANT_ID, JwksFake
from fakes.reader import FakeReader
from tidal.api.auth import query_validator_from_settings
from tidal.common.config import Settings
from tidal.common.errors import ConfigError, DependencyUnavailable
from tidal.query.service import create_query_app, remote_reader_from_settings

pytestmark = pytest.mark.anyio
AUD = "api://tidal-query"
API_CLIENT = "aaaaaaaa-0000-0000-0000-000000000001"
API_OID = "bbbbbbbb-0000-0000-0000-000000000002"
ROLE = "Tidal.Query.Read"
BASE = {
    "TIDAL_ENV": "local",
    "TIDAL_SERVICE_NAME": "query",
    "TIDAL_AUTH__TENANT_ID": TEST_TENANT_ID,
    "TIDAL_AUTH__API_CLIENT_ID": API_CLIENT,
    "TIDAL_AUTH__API_OBJECT_ID": API_OID,
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Token:
    def __init__(self, value: str) -> None:
        self.value = value

    async def token(self, audience: str) -> str:
        return self.value


async def call(jwks: JwksFake, token: str, env: dict[str, str] | None = None) -> None:
    settings = Settings.from_env(env or BASE)
    validator = query_validator_from_settings(settings, jwks.async_client())
    app = create_query_app(FakeReader(), validator, settings)
    reader = remote_reader_from_settings(
        settings, tokens=Token(token), transport=httpx.ASGITransport(app=app)
    )
    await reader.ping()


def mint(
    jwks: JwksFake,
    *,
    roles: list[str] | None = None,
    azp: str | None = API_CLIENT,
    oid: str = API_OID,
) -> str:
    return jwks.mint_token(
        aud=AUD,
        roles=[ROLE] if roles is None else roles,
        oid=oid,
        extra_claims={"azp": azp} if azp else {},
    )


async def test_id_api_is_accepted(jwks: JwksFake) -> None:
    await call(jwks, mint(jwks))


REFUSED: dict[str, Callable[[JwksFake], str]] = {
    "audience": lambda j: j.mint_token(aud="api://tidal", roles=[ROLE], oid=API_OID),
    "end-user-role": lambda j: mint(j, roles=["Tidal.Reader"]),
    "no-role": lambda j: mint(j, roles=[]),
    "other-app": lambda j: mint(j, azp="cccccccc-0000-0000-0000-000000000003"),
    "no-azp": lambda j: mint(j, azp=None),
    "other-oid": lambda j: mint(j, oid="dddddddd-0000-0000-0000-000000000004"),
    "expired": lambda j: j.expired_token(aud=AUD, roles=[ROLE]),
    "alg-none": lambda j: j.alg_none_token(aud=AUD, roles=[ROLE]),
}


@pytest.mark.parametrize("name", list(REFUSED))
async def test_other_callers_are_refused(jwks: JwksFake, name: str) -> None:
    with pytest.raises(DependencyUnavailable):  # 401/403 surface to the api as 503
        await call(jwks, REFUSED[name](jwks))


async def test_appid_claim_and_oid_only_config(jwks: JwksFake) -> None:
    v1 = jwks.mint_token(aud=AUD, roles=[ROLE], oid=API_OID, extra_claims={"appid": API_CLIENT})
    await call(jwks, v1)
    only_oid = {k: v for k, v in BASE.items() if k != "TIDAL_AUTH__API_CLIENT_ID"}
    await call(jwks, mint(jwks, azp=None), only_oid)


def test_caller_identity_is_required() -> None:
    env = {k: v for k, v in BASE.items() if "API_" not in k}
    with pytest.raises(ConfigError):
        query_validator_from_settings(Settings.from_env(env))
