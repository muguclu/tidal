"""``EntraTokenValidator``: tidal-query accepts only allowlisted callers with the role (T-402)."""

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
QUERY_APP = "eeeeeeee-0000-0000-0000-000000000005"
OTHER_APP = "ffffffff-0000-0000-0000-000000000006"
API_CLIENT = "aaaaaaaa-0000-0000-0000-000000000001"
WORKER_CLIENT = "aaaaaaaa-0000-0000-0000-000000000007"
API_OID = "bbbbbbbb-0000-0000-0000-000000000002"
UNKNOWN = "cccccccc-0000-0000-0000-000000000003"
ROLE = "Tidal.Query.Read"
BASE = {
    "TIDAL_ENV": "local",
    "TIDAL_SERVICE_NAME": "query",
    "TIDAL_AUTH__TENANT_ID": TEST_TENANT_ID,
    "TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS": f"{API_CLIENT}, {WORKER_CLIENT.upper()}",
    "TIDAL_AUTH__QUERY_CALLER_OBJECT_IDS": API_OID,
    "TIDAL_QUERY__APP_CLIENT_ID": QUERY_APP,
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
    oid: str = UNKNOWN,
    aud: str = AUD,
) -> str:
    return jwks.mint_token(
        aud=aud,
        roles=[ROLE] if roles is None else roles,
        oid=oid,
        extra_claims=({"azp": azp} if azp else {}) | {"idtyp": "app"},
    )


async def test_id_api_and_id_worker_are_accepted(jwks: JwksFake) -> None:
    await call(jwks, mint(jwks, azp=API_CLIENT))
    await call(jwks, mint(jwks, azp=WORKER_CLIENT))  # case-insensitive allowlist


async def test_object_id_allowlist_and_client_guid_audience(jwks: JwksFake) -> None:
    await call(jwks, mint(jwks, azp=None, oid=API_OID))
    await call(jwks, mint(jwks, aud=QUERY_APP))  # v2 tokens carry the app client id


REFUSED: dict[str, Callable[[JwksFake], str]] = {
    "audience": lambda j: j.mint_token(aud="api://tidal", roles=[ROLE], oid=API_OID),
    "other-app-guid": lambda j: mint(j, aud=OTHER_APP),
    "unknown-mi-with-role": lambda j: mint(j, azp=UNKNOWN),
    "allowed-mi-without-role": lambda j: mint(j, roles=[]),
    "allowed-mi-with-end-user-role": lambda j: mint(j, roles=["Tidal.Reader"]),
    "user-token-scp": lambda j: j.mint_token(
        aud=AUD,
        roles=[ROLE],
        extra_claims={"azp": API_CLIENT, "idtyp": "app", "scp": "access_as_user"},
    ),
    "no-idtyp": lambda j: j.mint_token(aud=AUD, roles=[ROLE], extra_claims={"azp": API_CLIENT}),
    "no-azp-unknown-oid": lambda j: mint(j, azp=None),
    "expired": lambda j: j.expired_token(aud=AUD, roles=[ROLE]),
    "alg-none": lambda j: j.alg_none_token(aud=AUD, roles=[ROLE]),
}


@pytest.mark.parametrize("name", list(REFUSED))
async def test_other_callers_are_refused(jwks: JwksFake, name: str) -> None:
    with pytest.raises(DependencyUnavailable):  # 401/403 surface to the api as 503
        await call(jwks, REFUSED[name](jwks))


async def test_appid_claim_is_accepted(jwks: JwksFake) -> None:
    v1 = jwks.mint_token(aud=AUD, roles=[ROLE], extra_claims={"appid": API_CLIENT, "idtyp": "app"})
    await call(jwks, v1)


def test_empty_allowlists_are_a_startup_error() -> None:
    env = {k: v for k, v in BASE.items() if "CALLER" not in k}
    with pytest.raises(ConfigError):
        query_validator_from_settings(Settings.from_env(env))
    dev = {
        "TIDAL_ENV": "dev",
        "TIDAL_SERVICE_NAME": "query",
        "TIDAL_AUTH__TENANT_ID": TEST_TENANT_ID,
        "TIDAL_SQL__AUTH": "entra_mi",
        "TIDAL_SQL__SERVER": "x.database.windows.net",
        "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
    }
    with pytest.raises(ConfigError, match="QUERY_CALLER"):
        Settings.from_env(dev)
    Settings.from_env({**dev, "TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS": API_CLIENT})


def test_allowlists_must_be_guids() -> None:
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**BASE, "TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS": "not-a-guid"})
    assert "not-a-guid" not in str(exc.value)
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, "TIDAL_QUERY__APP_CLIENT_ID": "nope"})


async def status_of(jwks: JwksFake, token: str) -> int:
    settings = Settings.from_env(BASE)
    validator = query_validator_from_settings(settings, jwks.async_client())
    app = create_query_app(FakeReader(), validator, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://q"
    ) as raw:
        r = await raw.get("/internal/v1/ping", headers={"Authorization": f"Bearer {token}"})
    return r.status_code


async def test_401_for_bad_tokens_and_403_for_wrong_callers(jwks: JwksFake) -> None:
    assert await status_of(jwks, mint(jwks)) == 204
    assert await status_of(jwks, REFUSED["expired"](jwks)) == 401
    assert await status_of(jwks, REFUSED["user-token-scp"](jwks)) == 401
    assert await status_of(jwks, REFUSED["no-idtyp"](jwks)) == 401
    assert await status_of(jwks, REFUSED["allowed-mi-without-role"](jwks)) == 403
    assert await status_of(jwks, REFUSED["unknown-mi-with-role"](jwks)) == 403
