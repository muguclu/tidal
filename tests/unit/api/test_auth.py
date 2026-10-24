"""Entra JWT validation, role enforcement (G4 matrix), JWKS rotation, cursor key (T-402).

Tokens are signed by the generated test RSA keys of ``fakes.jwks`` and served from a mock JWKS
transport; no network is used.
"""

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from fakes.backtest_store import FakeBacktestStore
from fakes.bus import FakeBus
from fakes.jwks.core import TEST_TENANT_ID, JwksFake, KeyPair
from fakes.reader import FakeReader
from tidal.api.app import create_app
from tidal.api.auth import (
    JwksCache,
    JwtVerifier,
    api_validator_from_settings,
    resolve_cursor_key,
)
from tidal.common.config import Settings
from tidal.common.errors import ConfigError, DependencyUnavailable, Unauthenticated

pytestmark = pytest.mark.anyio

READER, TRADER, QUANT, ADMIN = "Tidal.Reader", "Tidal.Trader", "Tidal.Quant", "Tidal.Admin"
ENV = {"TIDAL_ENV": "local", "TIDAL_AUTH__TENANT_ID": TEST_TENANT_ID}
BODY = {
    "strategy": "ma_crossover",
    "instrument_ids": ["EIA.WTI.SPOT"],
    "params": {"fast": 5, "slow": 20},
    "interval": "1d",
    "start": "2020-01-01T00:00:00Z",
    "end": "2021-01-01T00:00:00Z",
    "costs": {"fee_bps": 1, "slippage_bps": 1},
    "seed": 1,
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def app(jwks: JwksFake) -> FastAPI:
    settings = Settings.from_env(ENV)
    application = create_app(
        settings,
        reader=FakeReader(),
        bus=FakeBus(),
        token_validator=api_validator_from_settings(settings, jwks.async_client()),
    )
    application.state.backtest_store = FakeBacktestStore()
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- negative token cases -----------------------------------------------------------------------
async def test_missing_token_is_401(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/instruments")
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["correlation_id"]


NEGATIVE: dict[str, Callable[[JwksFake], str]] = {
    "expired": lambda j: j.expired_token(roles=[READER]),
    "nbf": lambda j: j.not_yet_valid_token(roles=[READER]),
    "aud": lambda j: j.wrong_audience_token(roles=[READER]),
    "iss": lambda j: j.wrong_issuer_token(roles=[READER]),
    "unknown-kid": lambda j: j.unknown_kid_token(roles=[READER]),
    "none": lambda j: j.alg_none_token(roles=[READER]),
    "hs256-pem": lambda j: j.hs256_confusion_token(roles=[READER]),
    "hs256-der": lambda j: j.hs256_confusion_der_token(roles=[READER]),
    "hs256-jwk": lambda j: j.hs256_confusion_jwk_token(roles=[READER]),
    "tid": lambda j: j.mint_token(roles=[READER], extra_claims={"tid": "other-tenant"}),
    "forged-signature": lambda j: j.mint_token(roles=[READER], key=j.rogue, kid=j.active.kid),
    "garbage": lambda _j: "not.a.jwt",
}


@pytest.mark.parametrize("name", list(NEGATIVE))
async def test_invalid_tokens_are_401(client: httpx.AsyncClient, jwks: JwksFake, name: str) -> None:
    token = NEGATIVE[name](jwks)
    r = await client.get("/v1/instruments", headers=bearer(token))
    assert r.status_code == 401
    assert token not in r.text


async def test_small_clock_skew_is_tolerated(client: httpx.AsyncClient, jwks: JwksFake) -> None:
    token = jwks.mint_token(roles=[READER], nbf_offset=10, exp_offset=3600)  # within 30 s leeway
    assert (await client.get("/v1/instruments", headers=bearer(token))).status_code == 200


async def test_missing_roles_claim_is_403(client: httpx.AsyncClient, jwks: JwksFake) -> None:
    r = await client.get("/v1/instruments", headers=bearer(jwks.missing_roles_token()))
    assert r.status_code == 403


async def test_unknown_roles_confer_nothing(client: httpx.AsyncClient, jwks: JwksFake) -> None:
    token = jwks.mint_token(roles=["Tidal.Superuser", "reader"])
    assert (await client.get("/v1/instruments", headers=bearer(token))).status_code == 403


# --- G4 role matrix -----------------------------------------------------------------------------
TICKS = "/v1/ticks?instrument_id=EIA.WTI.SPOT&start=2020-04-20T00:00:00Z&end=2020-04-20T00:01:00Z"
MATRIX = [
    # (roles, GET instruments (reader), GET ticks (trader), POST backtests (quant))
    ([READER], True, False, False),
    ([TRADER], True, True, False),
    ([QUANT], True, True, True),
    ([ADMIN], False, False, False),  # admin is separate: no data access
    ([ADMIN, READER], True, False, False),
]


@pytest.mark.parametrize("case", MATRIX)
async def test_role_matrix(
    client: httpx.AsyncClient, jwks: JwksFake, case: tuple[list[str], bool, bool, bool]
) -> None:
    roles, read, ticks, submit = case
    headers = bearer(jwks.mint_token(roles=roles))
    r_read = await client.get("/v1/instruments", headers=headers)
    r_ticks = await client.get(TICKS, headers=headers)
    r_submit = await client.post("/v1/backtests", json=BODY, headers=headers)
    assert (r_read.status_code == 200) is read
    assert (r_ticks.status_code != 403) is ticks
    assert (r_submit.status_code == 202) is submit
    for r, allowed in ((r_read, read), (r_ticks, ticks), (r_submit, submit)):
        if not allowed:
            assert r.status_code == 403


# --- JWKS cache / rotation ----------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def counting_client(doc: dict[str, Any], hits: list[int], status: int = 200) -> httpx.AsyncClient:
    def handler(_: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(status, json=doc)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_jwks_is_cached_and_rotation_is_picked_up(jwks: JwksFake) -> None:
    hits: list[int] = []
    doc: dict[str, Any] = {"keys": [jwks.active.public_jwk()]}
    clock = Clock()
    cache = JwksCache("https://keys.test/jwks", counting_client(doc, hits), monotonic=clock)
    verifier = JwtVerifier(TEST_TENANT_ID, "api://tidal", cache)
    for _ in range(3):
        await verifier.verify(jwks.mint_token(roles=[READER]))
    assert len(hits) == 1  # cached
    new = KeyPair.generate("rotated")
    doc["keys"] = [jwks.active.public_jwk(), new.public_jwk()]
    token = jwks.mint_token(roles=[READER], key=new)
    with pytest.raises(Unauthenticated):  # unknown kid, but refreshes are rate limited
        await verifier.verify(token)
    clock.t += 31
    assert (await verifier.verify(token))["tid"] == TEST_TENANT_ID
    assert len(hits) == 2


async def test_jwks_ttl_refresh_and_stale_keys_survive_outage(jwks: JwksFake) -> None:
    hits: list[int] = []
    clock = Clock()
    ok = counting_client({"keys": [jwks.active.public_jwk()]}, hits)
    cache = JwksCache("https://keys.test/jwks", ok, monotonic=clock)
    await cache.key(jwks.active.kid)
    clock.t += 3601
    cache._client = counting_client({}, hits, status=500)  # simulate an outage
    await cache.key(jwks.active.kid)  # refresh fails, stale key still served
    assert len(hits) == 2


async def test_jwks_unavailable_at_start_is_503() -> None:
    cache = JwksCache("https://keys.test/jwks", counting_client({}, [], status=500))
    with pytest.raises(DependencyUnavailable):
        await cache.key("any")


async def test_jwks_from_local_file(tmp_path: Path, jwks: JwksFake) -> None:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(jwks.jwks()), encoding="utf-8")
    verifier = JwtVerifier(TEST_TENANT_ID, "api://tidal", JwksCache(str(path)))
    assert await verifier.verify(jwks.mint_token(roles=[READER]))


# --- startup rules ------------------------------------------------------------------------------
DEV = {
    "TIDAL_ENV": "dev",
    "TIDAL_AUTH__TENANT_ID": TEST_TENANT_ID,
    "TIDAL_SQL__AUTH": "entra_mi",
    "TIDAL_SQL__SERVER": "x.database.windows.net",
    "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
}
VAULT = {**DEV, "TIDAL_CURSOR__KEYVAULT_URL": "https://kv-tidal.vault.azure.net"}


def test_jwks_url_and_cursor_key_refused_outside_local() -> None:
    with pytest.raises(ConfigError, match="TIDAL_AUTH__JWKS_URL"):
        Settings.from_env({**DEV, "TIDAL_AUTH__JWKS_URL": "file:///x"})
    Settings.from_env({**ENV, "TIDAL_AUTH__JWKS_URL": ".data/dev-jwks/jwks.json"})
    Settings.from_env(DEV)
    with pytest.raises(ConfigError, match="TIDAL_CURSOR__KEY"):
        Settings.from_env({**DEV, "TIDAL_CURSOR__KEY": "x" * 40})


# --- cursor key ---------------------------------------------------------------------------------
class FakeSecrets:
    def __init__(self, value: str | Exception) -> None:
        self.value = value
        self.asked: list[str] = []

    def get(self, name: str) -> str:
        self.asked.append(name)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_cursor_key_local_default_and_env() -> None:
    assert resolve_cursor_key(Settings.from_env({"TIDAL_ENV": "local"})) is None
    env = {"TIDAL_ENV": "local", "TIDAL_CURSOR__KEY": "k" * 32}
    assert resolve_cursor_key(Settings.from_env(env)) == b"k" * 32
    with pytest.raises(ConfigError):
        resolve_cursor_key(Settings.from_env({**env, "TIDAL_CURSOR__KEY": "short"}))


def test_cursor_key_from_key_vault_is_shared_across_replicas() -> None:
    secret = "s" * 48
    settings = Settings.from_env(VAULT)
    source = FakeSecrets(secret)
    a = create_app(settings, secret_source=source)
    b = create_app(settings, secret_source=FakeSecrets(secret))
    assert a.state.cursor_key == b.state.cursor_key == secret.encode()
    assert source.asked == ["cursor-hmac-key"]


def test_cursor_key_missing_or_bad_outside_local_fails_closed() -> None:
    with pytest.raises(ConfigError, match="KEYVAULT_URL"):
        create_app(Settings.from_env(DEV))
    with pytest.raises(ConfigError):
        create_app(Settings.from_env(VAULT), secret_source=FakeSecrets("short"))
    with pytest.raises(ConfigError) as exc:
        create_app(Settings.from_env(VAULT), secret_source=FakeSecrets(RuntimeError("tok=abc")))
    assert "abc" not in str(exc.value)


API_APP = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER_APP = "22222222-aaaa-bbbb-cccc-000000000002"


async def test_api_accepts_app_client_id_guid_audience(jwks: JwksFake) -> None:
    settings = Settings.from_env({**ENV, "TIDAL_AUTH__API_APP_CLIENT_ID": API_APP})
    app = create_app(
        settings,
        reader=FakeReader(),
        token_validator=api_validator_from_settings(settings, jwks.async_client()),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        ok = await c.get(
            "/v1/instruments", headers=bearer(jwks.mint_token(roles=[READER], aud=API_APP))
        )
        uri = await c.get("/v1/instruments", headers=bearer(jwks.mint_token(roles=[READER])))
        bad = await c.get(
            "/v1/instruments", headers=bearer(jwks.mint_token(roles=[READER], aud=OTHER_APP))
        )
    assert (ok.status_code, uri.status_code, bad.status_code) == (200, 200, 401)
