"""``/v1/admin/*`` over HTTP with real RS256 tokens (T-410, ADR-0008 R-20)."""

from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from fastapi import FastAPI

from fakes.bus import FakeBus
from fakes.jwks.core import TEST_TENANT_ID, JwksFake
from fakes.reader import FakeReader
from tidal.api.app import create_app
from tidal.api.auth import api_validator_from_settings
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.messages import TieringJobMessage
from tidal.contracts.models import StorageStats
from tidal.query.protocols import OutboundMessage

pytestmark = pytest.mark.anyio

CLI = "99999999-1111-2222-3333-444444444444"
ENV = {
    "TIDAL_ENV": "local",
    "TIDAL_AUTH__TENANT_ID": TEST_TENANT_ID,
    "TIDAL_AUTH__ALLOWED_CLIENT_IDS": CLI,
}
USER = {"scp": "access_as_user", "azp": CLI}
RUN, STATS = "/v1/admin/tiering/run", "/v1/admin/storage/stats"
SNAPSHOT = StorageStats.model_validate(
    {
        "as_of": "2026-10-01T00:00:00Z",
        "hot": {"rows": 10, "bytes": 100},
        "cold": {"rows": 20, "bytes": 200},
    }
)


class FakeStats:
    def __init__(self, stats: StorageStats | None) -> None:
        self.stats = stats

    async def latest(self) -> StorageStats | None:
        return self.stats


class DownBus:
    async def send_batch(self, messages: Sequence[OutboundMessage]) -> int:
        raise ConnectionError("down")

    async def ping(self) -> None:
        return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def app(jwks: JwksFake, bus: FakeBus) -> FastAPI:
    settings = Settings.from_env(ENV)
    application = create_app(
        settings,
        reader=FakeReader(),
        bus=bus,
        clock=FixedClock(1_700_000_000 * 10**9),
        token_validator=api_validator_from_settings(settings, jwks.async_client()),
    )
    application.state.storage_stats = FakeStats(SNAPSHOT)
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def auth(jwks: JwksFake, *roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jwks.mint_token(roles=list(roles), extra_claims=USER)}"}


@pytest.mark.parametrize("role", ["Tidal.Reader", "Tidal.Trader", "Tidal.Quant"])
@pytest.mark.parametrize(("method", "path"), [("POST", RUN), ("GET", STATS)])
async def test_data_roles_are_403(  # noqa: PLR0917 - parametrised matrix
    client: httpx.AsyncClient, jwks: JwksFake, bus: FakeBus, role: str, method: str, path: str
) -> None:
    r = await client.request(method, path, headers=auth(jwks, role))
    assert r.status_code == 403
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["correlation_id"]
    assert bus.sent == []


@pytest.mark.parametrize(("method", "path"), [("POST", RUN), ("GET", STATS)])
async def test_no_token_is_401(client: httpx.AsyncClient, method: str, path: str) -> None:
    assert (await client.request(method, path)).status_code == 401


async def test_admin_runs_tiering(client: httpx.AsyncClient, jwks: JwksFake, bus: FakeBus) -> None:
    r = await client.post(RUN, headers=auth(jwks, "Tidal.Admin"))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    sent = [m for m in bus.sent if isinstance(m, TieringJobMessage)]
    assert len(sent) == len(bus.sent) == 2
    assert [m.payload.table for m in sent] == ["md.bar_hot", "md.tick_hot"]
    assert all(body["run_id"] in m.message_id for m in sent)
    assert all(m.correlation_id == r.headers["x-correlation-id"] for m in sent)
    assert sent[0].payload.hot_window_days == 90
    assert not sent[0].payload.dry_run


async def test_admin_reads_stats(client: httpx.AsyncClient, jwks: JwksFake) -> None:
    r = await client.get(STATS, headers=auth(jwks, "Tidal.Admin"))
    assert r.status_code == 200
    assert r.json()["hot"] == {"rows": 10, "bytes": 100}
    assert r.json()["cold"] == {"rows": 20, "bytes": 200}


async def test_no_stats_run_yet_is_503(
    client: httpx.AsyncClient, jwks: JwksFake, app: FastAPI
) -> None:
    app.state.storage_stats = FakeStats(None)
    r = await client.get(STATS, headers=auth(jwks, "Tidal.Admin"))
    assert r.status_code == 503
    assert r.headers["content-type"] == "application/problem+json"


async def test_unconfigured_stats_source_is_503(
    client: httpx.AsyncClient, jwks: JwksFake, app: FastAPI
) -> None:
    del app.state.storage_stats
    assert (await client.get(STATS, headers=auth(jwks, "Tidal.Admin"))).status_code == 503


async def test_bus_failure_is_503(client: httpx.AsyncClient, jwks: JwksFake, app: FastAPI) -> None:
    app.state.bus = DownBus()
    assert (await client.post(RUN, headers=auth(jwks, "Tidal.Admin"))).status_code == 503
