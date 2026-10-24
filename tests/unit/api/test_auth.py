"""Entra JWT validation, role enforcement (G4 matrix), JWKS rotation, cursor key (T-402).

Tokens are signed by the generated test RSA keys of ``fakes.jwks`` and served from a mock JWKS
transport; no network is used.
"""

import asyncio
import base64
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
from tidal.common.errors import (
    ConfigError,
    DependencyUnavailable,
    Unauthenticated,
    ValidationFailed,
)

pytestmark = pytest.mark.anyio

READER, TRADER, QUANT, ADMIN = "Tidal.Reader", "Tidal.Trader", "Tidal.Quant", "Tidal.Admin"
CLI = "99999999-1111-2222-3333-444444444444"
ENV = {
    "TIDAL_ENV": "local",
    "TIDAL_AUTH__TENANT_ID": TEST_TENANT_ID,
    "TIDAL_AUTH__ALLOWED_CLIENT_IDS": CLI,
}
USER = {"scp": "access_as_user", "azp": CLI}


def user(j: JwksFake, **kw: Any) -> str:
    """A delegated user token from the allowed client."""
    kw["extra_claims"] = USER | kw.get("extra_claims", {})
    return j.mint_token(**kw)


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
    "expired": lambda j: j.expired_token(roles=[READER], extra_claims=USER),
    "nbf": lambda j: j.not_yet_valid_token(roles=[READER], extra_claims=USER),
    "aud": lambda j: j.wrong_audience_token(roles=[READER], extra_claims=USER),
    "iss": lambda j: j.wrong_issuer_token(roles=[READER], extra_claims=USER),
    "unknown-kid": lambda j: j.unknown_kid_token(roles=[READER], extra_claims=USER),
    "none": lambda j: j.alg_none_token(roles=[READER], extra_claims=USER),
    "hs256-pem": lambda j: j.hs256_confusion_token(roles=[READER], extra_claims=USER),
    "hs256-der": lambda j: j.hs256_confusion_der_token(roles=[READER], extra_claims=USER),
    "hs256-jwk": lambda j: j.hs256_confusion_jwk_token(roles=[READER], extra_claims=USER),
    "tid": lambda j: user(j, roles=[READER], extra_claims={"tid": "other-tenant"}),
    "forged-signature": lambda j: user(j, roles=[READER], key=j.rogue, kid=j.active.kid),
    "garbage": lambda _j: "not.a.jwt",
}


@pytest.mark.parametrize("name", list(NEGATIVE))
async def test_invalid_tokens_are_401(client: httpx.AsyncClient, jwks: JwksFake, name: str) -> None:
    token = NEGATIVE[name](jwks)
    r = await client.get("/v1/instruments", headers=bearer(token))
    assert r.status_code == 401
    assert token not in r.text


async def test_small_clock_skew_is_tolerated(client: httpx.AsyncClient, jwks: JwksFake) -> None:
    token = user(jwks, roles=[READER], nbf_offset=10, exp_offset=3600)  # within 30 s leeway
    assert (await client.get("/v1/instruments", headers=bearer(token))).status_code == 200


async def test_missing_roles_claim_is_403(client: httpx.AsyncClient, jwks: JwksFake) -> None:
    r = await client.get(
        "/v1/instruments", headers=bearer(jwks.missing_roles_token(extra_claims=USER))
    )
    assert r.status_code == 403


async def test_unknown_roles_confer_nothing(client: httpx.AsyncClient, jwks: JwksFake) -> None:
    token = user(jwks, roles=["Tidal.Superuser", "reader"])
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
    headers = bearer(user(jwks, roles=roles))
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
        await verifier.verify(user(jwks, roles=[READER]))
    assert len(hits) == 1  # cached
    new = KeyPair.generate("rotated")
    doc["keys"] = [jwks.active.public_jwk(), new.public_jwk()]
    token = user(jwks, roles=[READER], key=new)
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
    assert await verifier.verify(user(jwks, roles=[READER]))


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
def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


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
    env = {"TIDAL_ENV": "local", "TIDAL_CURSOR__KEY": b64(b"k" * 32)}
    assert resolve_cursor_key(Settings.from_env(env)) == b"k" * 32
    assert resolve_cursor_key(Settings.from_env(env)).previous == ()  # type: ignore[union-attr]
    with pytest.raises(ConfigError):
        resolve_cursor_key(Settings.from_env({**env, "TIDAL_CURSOR__KEY": b64(b"short")}))


def test_cursor_key_from_key_vault_is_shared_across_replicas() -> None:
    raw = b"s" * 48
    secret = b64(raw)
    settings = Settings.from_env(VAULT)
    source = FakeSecrets(secret)
    a = create_app(settings, secret_source=source)
    b = create_app(settings, secret_source=FakeSecrets(secret))
    assert a.state.cursor_key == b.state.cursor_key == raw
    assert source.asked == ["cursor-hmac-key", "cursor-hmac-key-previous"]


def test_cursor_key_missing_or_bad_outside_local_fails_closed() -> None:
    with pytest.raises(ConfigError, match="KEYVAULT_URL"):
        create_app(Settings.from_env(DEV))
    with pytest.raises(ConfigError):
        create_app(Settings.from_env(VAULT), secret_source=FakeSecrets(b64(b"short")))
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
        ok = await c.get("/v1/instruments", headers=bearer(user(jwks, roles=[READER], aud=API_APP)))
        uri = await c.get("/v1/instruments", headers=bearer(user(jwks, roles=[READER])))
        bad = await c.get(
            "/v1/instruments", headers=bearer(user(jwks, roles=[READER], aud=OTHER_APP))
        )
    assert (ok.status_code, uri.status_code, bad.status_code) == (200, 200, 401)


def test_cursor_key_rotation_previous_verifies_but_never_signs() -> None:
    from tidal.api.pagination import Position, decode_cursor, encode_cursor  # noqa: PLC0415

    class Two:
        def __init__(self, cur: bytes, old: bytes | None) -> None:
            self.vals = {"cursor-hmac-key": b64(cur)}
            if old:
                self.vals["cursor-hmac-key-previous"] = b64(old)

        def get(self, name: str) -> str:
            return self.vals[name]

    old, new = b"o" * 32, b"n" * 32
    settings = Settings.from_env(VAULT)
    before = resolve_cursor_key(settings, Two(old, None))
    after = resolve_cursor_key(settings, Two(new, old))
    assert before is not None
    assert after is not None
    assert after.previous == (old,)
    cursor = encode_cursor(before, "b", Position(1, 2))
    assert decode_cursor(after, "b", cursor) == Position(1, 2)  # old cursor still verifies
    fresh = encode_cursor(after, "b", Position(1, 2))
    with pytest.raises(ValidationFailed):
        decode_cursor(before, "b", fresh)  # signed with the current key only


# --- token type, required claims, JWKS hardening ------------------------------------------------
@pytest.mark.parametrize(
    "claims",
    [
        {"scp": "other_scope"},
        {"scp": ""},
        {"azp": "88888888-1111-2222-3333-444444444444"},  # client not on the allowlist
    ],
)
async def test_api_needs_user_scope_and_allowed_client(
    client: httpx.AsyncClient, jwks: JwksFake, claims: dict[str, str]
) -> None:
    token = user(jwks, roles=[READER], extra_claims=claims)
    assert (await client.get("/v1/instruments", headers=bearer(token))).status_code == 401


async def test_api_rejects_app_tokens_without_scp_or_azp(
    client: httpx.AsyncClient, jwks: JwksFake
) -> None:
    ok = jwks.mint_token(roles=[READER], azp=CLI)  # user token: scp + allowed client
    assert (await client.get("/v1/instruments", headers=bearer(ok))).status_code == 200
    no_scp = jwks.mint_token(roles=[READER], scp="", azp=CLI)  # allowed azp, no scope
    assert (await client.get("/v1/instruments", headers=bearer(no_scp))).status_code == 401
    no_azp = jwks.mint_token(roles=[READER], azp="")  # scope present, but no client id
    assert (await client.get("/v1/instruments", headers=bearer(no_azp))).status_code == 401


@pytest.mark.parametrize("missing", ["exp", "nbf", "oid", "tid"])
async def test_required_claims_must_be_present(jwks: JwksFake, missing: str) -> None:
    import jwt  # noqa: PLC0415

    token = user(jwks, roles=[READER])
    claims = jwt.decode(token, options={"verify_signature": False})
    del claims[missing]
    forged = jwt.encode(
        claims, jwks.active.private_pem(), algorithm="RS256", headers={"kid": jwks.active.kid}
    )
    verifier = JwtVerifier(TEST_TENANT_ID, "api://tidal", JwksCache(str(jwks_file(jwks))))
    with pytest.raises(Unauthenticated):
        await verifier.verify(forged)


def jwks_file(jwks: JwksFake, tmp: Path | None = None) -> Path:
    import tempfile  # noqa: PLC0415

    path = Path(tempfile.mkdtemp()) / "jwks.json"
    path.write_text(json.dumps(jwks.jwks()), encoding="utf-8")
    return path


async def test_non_rsa_jwk_is_never_used(jwks: JwksFake) -> None:
    ec = {"kty": "EC", "kid": "ec-key", "crv": "P-256", "x": "AA", "y": "AA"}
    doc = {"keys": [ec, jwks.active.public_jwk()]}
    cache = JwksCache("https://keys.test/jwks", counting_client(doc, []))
    assert await cache.key(jwks.active.kid)
    with pytest.raises(Unauthenticated):
        await cache.key("ec-key")


async def test_jwks_document_is_capped_at_64_keys(jwks: JwksFake) -> None:
    keys = [{**jwks.active.public_jwk(), "kid": f"k{i}"} for i in range(100)]
    cache = JwksCache("https://keys.test/jwks", counting_client({"keys": keys}, []))
    assert await cache.key("k63")
    with pytest.raises(Unauthenticated):
        await cache.key("k64")


async def test_known_kid_is_not_blocked_by_a_slow_refresh(jwks: JwksFake) -> None:
    release = asyncio.Event()
    calls: list[int] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) > 1:
            await release.wait()  # the identity provider hangs on every refresh after the first
        return httpx.Response(200, json={"keys": [jwks.active.public_jwk()]})

    clock = Clock()
    cache = JwksCache(
        "https://keys.test/jwks",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        monotonic=clock,
    )
    await cache.key(jwks.active.kid)
    clock.t += 31
    stuck = asyncio.create_task(cache.key("unknown-kid"))  # starts the slow refresh
    await asyncio.sleep(0.05)
    fast = await asyncio.wait_for(
        asyncio.gather(*(cache.key(jwks.active.kid) for _ in range(20))), timeout=1
    )
    assert len(fast) == 20
    assert not stuck.done()
    release.set()
    with pytest.raises(Unauthenticated):
        await stuck
    assert len(calls) == 2  # one shared refresh


def test_azure_credential_is_pinned_and_requires_client_id() -> None:
    from tidal.api.auth import azure_client_id, build_credential  # noqa: PLC0415

    with pytest.raises(ConfigError, match="AZURE_CLIENT_ID"):
        azure_client_id({})
    with pytest.raises(ConfigError):
        build_credential(environ={"AZURE_CLIENT_ID": " "})
    cred = build_credential(environ={"AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000009"})
    assert type(cred).__name__ == "ChainedTokenCredential"
    names = [type(c).__name__ for c in cred.credentials]
    assert names[-1] == "ManagedIdentityCredential"
    assert "DefaultAzureCredential" not in names
