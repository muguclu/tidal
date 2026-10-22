"""API skeleton: health, problem+json, correlation id, auth seam, OpenAPI export."""

import asyncio
import json
import logging
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

import tidal.api.app as app_module
from fakes.bus import FakeBus
from fakes.reader import SEEDED_IDS, WTI_NEGATIVE_DAY, FakeReader
from tidal.api.app import create_app
from tidal.api.deps import LOCAL_ANONYMOUS, Principal, get_bus, get_clock, get_principal, get_reader
from tidal.api.routers import health
from tidal.common.config import Settings, get_settings
from tidal.common.errors import (
    ConfigError,
    NotFound,
    RangeTooLarge,
    RateLimited,
    ValidationFailed,
)
from tidal.contracts.models import Tier
from tidal.contracts.schemas import BAR_V1, Interval
from tidal.query.protocols import Bus, Clock, Reader

MakeClient = Callable[[FastAPI], httpx.AsyncClient]
pytestmark = pytest.mark.anyio
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
PROBLEM = "application/problem+json"


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only."""
    return "asyncio"


@pytest.fixture
def reader() -> FakeReader:
    """Healthy seeded reader."""
    return FakeReader()


@pytest.fixture
def bus() -> FakeBus:
    """Healthy bus."""
    return FakeBus()


@pytest.fixture
def app(reader: FakeReader, bus: FakeBus) -> FastAPI:
    """App with fakes, local env, auth disabled."""
    settings = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
    return create_app(settings, reader=reader, bus=bus)


@pytest.fixture
def make_client() -> Callable[[FastAPI], httpx.AsyncClient]:
    """Build an AsyncClient for an app; unhandled errors become 500 responses, not raises."""

    def build(app: FastAPI) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return build


@pytest.fixture
async def client(
    app: FastAPI, make_client: Callable[[FastAPI], httpx.AsyncClient]
) -> AsyncIterator[httpx.AsyncClient]:
    """Client for ``app``."""
    async with make_client(app) as c:
        yield c


class Payload(BaseModel):
    """Body model for the validation test."""

    qty: int = Field(ge=1)


def add_test_routes(app: FastAPI) -> None:
    """Routes that provoke each error class."""

    @app.get("/t/range")
    async def _range() -> None:
        raise RangeTooLarge("Range exceeds the limit.")

    @app.get("/t/rate")
    async def _rate() -> None:
        raise RateLimited(7)

    @app.get("/t/boom")
    async def _boom() -> None:
        raise RuntimeError("secret-internal-detail password=hunter2")

    @app.get("/t/domain")
    async def _domain() -> None:
        raise ValidationFailed("Bad thing.")

    @app.get("/t/limit")
    async def _limit(limit: int = 5) -> dict[str, int]:
        return {"limit": limit}

    @app.post("/t/map")
    async def _map(payload: dict[str, int]) -> dict[str, int]:
        return payload

    @app.post("/t/body")
    async def _body(payload: Payload) -> Payload:
        return payload

    @app.get("/t/me")
    async def _me(p: Principal = Depends(get_principal)) -> dict[str, str]:  # noqa: B008
        return {"oid": p.oid}


async def test_live(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


@pytest.mark.parametrize(("bad_reader", "bad_bus"), [(True, False), (False, True), (True, True)])
async def test_ready_failing_dependency_hides_details(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture, bad_reader: bool, bad_bus: bool
) -> None:
    app = create_app(
        Settings.from_env({"TIDAL_ENV": "local"}),
        reader=FakeReader(fail_ping=bad_reader),
        bus=FakeBus(fail_ping=bad_bus),
    )
    async with make_client(app) as c:
        r = await c.get("/v1/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready"}
    assert "fake" not in r.text
    assert "readiness_check_failed" in caplog.text


async def test_ready_without_clients_is_not_ready(make_client: MakeClient) -> None:
    async with make_client(create_app(Settings.from_env({"TIDAL_ENV": "local"}))) as c:
        r = await c.get("/v1/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready"}


async def test_unknown_path_is_404_problem(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/nope")
    assert r.status_code == 404
    assert r.headers["content-type"] == PROBLEM
    body = r.json()
    assert body["status"] == 404
    assert body["type"].endswith("/not-found")
    assert body["correlation_id"] == r.headers["x-correlation-id"]
    assert "detail" in body


async def test_wrong_method_is_problem_with_allow_header(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/health/live")
    assert r.status_code == 405
    assert r.headers["content-type"] == PROBLEM
    assert "GET" in r.headers["allow"]
    assert r.headers["x-correlation-id"] == r.json()["correlation_id"]


async def test_413_problem(app: FastAPI, client: httpx.AsyncClient) -> None:
    add_test_routes(app)
    r = await client.get("/t/range")
    assert r.status_code == 413
    assert r.headers["content-type"] == PROBLEM
    assert r.json()["type"].endswith("/range-too-large")


async def test_429_sets_retry_after(app: FastAPI, client: httpx.AsyncClient) -> None:
    add_test_routes(app)
    r = await client.get("/t/rate")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "7"
    assert r.headers["content-type"] == PROBLEM
    assert "detail" not in r.json()  # exclude_none


async def test_400_not_422_and_no_input_echo(app: FastAPI, client: httpx.AsyncClient) -> None:
    add_test_routes(app)
    r = await client.get("/t/limit", params={"limit": "SENSITIVE-VALUE-123"})
    assert r.status_code == 400
    assert r.headers["content-type"] == PROBLEM
    assert "SENSITIVE-VALUE-123" not in r.text
    assert "query.limit" in r.json()["detail"]

    r = await client.post("/t/body", json={"qty": -99999, "extra<script>": "TOPSECRET"})
    assert r.status_code == 400
    assert "99999" not in r.text
    assert "TOPSECRET" not in r.text
    assert "<script>" not in r.text


async def test_domain_validation_error_is_400(app: FastAPI, client: httpx.AsyncClient) -> None:
    add_test_routes(app)
    r = await client.get("/t/domain")
    assert r.status_code == 400
    assert r.json()["detail"] == "Bad thing."


async def test_unhandled_exception_is_generic_500(
    app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    add_test_routes(app)
    r = await client.get("/t/boom")
    assert r.status_code == 500
    assert r.headers["content-type"] == PROBLEM
    assert r.headers["x-correlation-id"] == r.json()["correlation_id"]
    assert "secret-internal-detail" not in r.text
    assert "hunter2" not in r.text
    record = next(r for r in caplog.records if isinstance(r.msg, dict))
    handler = next(h for h in logging.getLogger().handlers if type(h).__name__ == "_TidalHandler")
    assert handler.formatter is not None
    rendered = handler.formatter.format(record)  # what reaches stdout
    assert json.loads(rendered)["event"] == "unhandled_error"
    assert "hunter2" not in rendered


async def test_correlation_id_generated_and_unique(client: httpx.AsyncClient) -> None:
    a = (await client.get("/v1/health/live")).headers["x-correlation-id"]
    b = (await client.get("/v1/health/live")).headers["x-correlation-id"]
    assert ULID_RE.match(a)
    assert a != b


async def test_correlation_id_echoed(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/health/live", headers={"X-Correlation-Id": "req-abc.123"})
    assert r.headers["x-correlation-id"] == "req-abc.123"
    r = await client.get("/v1/nope", headers={"X-Correlation-Id": "req-abc.123"})
    assert r.json()["correlation_id"] == "req-abc.123"


async def test_unsafe_correlation_id_replaced(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/health/live", headers={"X-Correlation-Id": "bad id;" + "x" * 100})
    assert ULID_RE.match(r.headers["x-correlation-id"])


async def test_correlation_id_in_logs(
    app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    app.state.reader = FakeReader(fail_ping=True)
    await client.get("/v1/health/ready", headers={"X-Correlation-Id": "trace-2"})
    events = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    line = next(e for e in events if e["event"] == "readiness_check_failed")
    assert line["correlation_id"] == "trace-2"
    assert line["dependency"] == "reader"


async def test_providers_return_injected_clients(
    app: FastAPI, client: httpx.AsyncClient, reader: FakeReader, bus: FakeBus
) -> None:
    @app.get("/t/deps")
    async def _deps(
        r: Reader = Depends(get_reader),  # noqa: B008
        b: Bus = Depends(get_bus),  # noqa: B008
        c: Clock = Depends(get_clock),  # noqa: B008
    ) -> dict[str, bool]:
        assert r is reader
        assert b is bus
        return {"tz": c.now().tzinfo is not None}

    assert (await client.get("/t/deps")).json() == {"tz": True}


async def test_missing_clients_give_503(make_client: MakeClient) -> None:
    app = create_app(Settings.from_env({"TIDAL_ENV": "local"}))

    @app.get("/t/r")
    async def _r(r: Reader = Depends(get_reader)) -> None:  # noqa: B008
        return None

    @app.get("/t/b")
    async def _b(b: Bus = Depends(get_bus)) -> None:  # noqa: B008
        return None

    async with make_client(app) as c:
        for path in ("/t/r", "/t/b"):
            r = await c.get(path)
            assert r.status_code == 503
            assert r.headers["content-type"] == PROBLEM


async def test_fake_reader_seed(reader: FakeReader) -> None:
    ns = int(WTI_NEGATIVE_DAY.timestamp()) * 1_000_000_000
    got = await reader.read_bars("EIA.WTI.SPOT", Interval.D1, ns, ns + 1)
    assert got.tier is Tier.COLD
    assert [b.close for b in got.rows] == [-37.63]
    assert {i.instrument_id for i in await reader.instruments()} == set(SEEDED_IDS)
    assert len(await reader.latest(["EIA.WTI.SPOT", "NOPE.X.Y"])) == 1
    assert (await reader.read_ticks("EIA.WTI.SPOT", 0, ns)).rows == []
    assert await reader.read_bars("EIA.WTI.SPOT", Interval.H1, 0, ns + 1) is not None


async def test_fake_bus_records(bus: FakeBus) -> None:
    assert await bus.send_batch([]) == 0
    assert bus.sent == []


async def test_principal_anonymous_only_in_local_with_auth_disabled(
    make_client: MakeClient,
) -> None:
    local = create_app(Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"}))
    add_test_routes(local)
    async with make_client(local) as c:
        r = await c.get("/t/me")
    assert r.json() == {"oid": LOCAL_ANONYMOUS.oid}

    enabled = create_app(Settings.from_env({"TIDAL_ENV": "local"}))
    add_test_routes(enabled)
    async with make_client(enabled) as c:
        no_token = await c.get("/t/me")
        with_token = await c.get("/t/me", headers={"Authorization": "Bearer abc.def.ghi"})
    assert no_token.status_code == 401
    assert with_token.status_code == 401  # seam until T-402: fail closed
    assert "abc.def.ghi" not in with_token.text


def test_auth_disabled_rejected_outside_local() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"TIDAL_ENV": "dev", "TIDAL_AUTH__DISABLED": "true"})
    # Defence in depth: create_app re-asserts even if settings validation was bypassed.
    bypassed = Settings.model_construct(
        env="prod",
        auth=Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"}).auth,
    )
    with pytest.raises(ConfigError):
        create_app(bypassed)


def test_principal_is_hashable_value() -> None:
    assert Principal("a", frozenset()) == Principal("a", frozenset())


def test_export_openapi_script(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    script = Path(__file__).parents[3] / "scripts" / "export_openapi.py"
    subprocess.run([sys.executable, str(script), str(out)], check=True)  # noqa: S603
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert {"/v1/health/live", "/v1/health/ready"} <= set(doc["paths"])
    assert doc["paths"]["/v1/health/ready"]["get"]["operationId"] == "getHealthReady"
    bad = subprocess.run([sys.executable, str(script)], check=False)  # noqa: S603
    assert bad.returncode == 2


def test_run_starts_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda a, **kw: calls.append((a, kw)))
    monkeypatch.setenv("TIDAL_ENV", "local")  # required (config-contract v1.1)
    get_settings.cache_clear()
    try:
        app_module.run()
    finally:
        get_settings.cache_clear()
    assert isinstance(calls[0][0], FastAPI)
    assert calls[0][1]["port"] == 8000


def test_generated_health_matches_contract() -> None:
    """Cheap in-process guard; the authoritative check is oasdiff in CI."""
    contract = yaml.safe_load(
        (Path(__file__).parents[3] / "docs/contracts/openapi.yaml").read_text(encoding="utf-8")
    )
    generated = create_app(Settings.from_env({"TIDAL_ENV": "local"})).openapi()
    for path in ("/v1/health/live", "/v1/health/ready"):
        want, got = contract["paths"][path]["get"], generated["paths"][path]["get"]
        assert set(got["responses"]) == set(want["responses"])
        assert got["operationId"] == want["operationId"]
        assert got["security"] == want["security"]
        for code, resp in got["responses"].items():
            assert "X-Correlation-Id" in resp["headers"] or "x-correlation-id" in resp["headers"]
            media = set(resp["content"])
            assert media == (
                {"application/problem+json"} if code == "429" else {"application/json"}
            )


class HangingReader(FakeReader):
    """Reader whose ping never resolves."""

    async def ping(self) -> None:
        await asyncio.Event().wait()


async def test_ready_times_out_hanging_dependency(
    monkeypatch: pytest.MonkeyPatch, make_client: MakeClient
) -> None:
    monkeypatch.setattr(health, "PING_TIMEOUT_S", 0.01)
    app = create_app(
        Settings.from_env({"TIDAL_ENV": "local"}), reader=HangingReader(), bus=FakeBus()
    )
    async with make_client(app) as c:
        r = await c.get("/v1/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready"}


async def test_401_www_authenticate(make_client: MakeClient) -> None:
    app = create_app(Settings.from_env({"TIDAL_ENV": "local"}))
    add_test_routes(app)
    async with make_client(app) as c:
        none = await c.get("/t/me")
        bad = await c.get("/t/me", headers={"Authorization": "Bearer abc.def.ghi"})
    assert none.headers["www-authenticate"] == "Bearer"
    assert bad.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_400_does_not_echo_body_keys(app: FastAPI, client: httpx.AsyncClient) -> None:
    add_test_routes(app)
    r = await client.post("/t/map", json={"inventedKeyName": "x"})
    assert r.status_code == 400
    assert "inventedKeyName" not in r.text
    assert "body" in r.json()["detail"]


async def test_docs_only_in_local(make_client: MakeClient) -> None:
    dev = Settings.from_env(
        {
            "TIDAL_ENV": "dev",
            "TIDAL_AUTH__TENANT_ID": "11111111-1111-1111-1111-111111111111",
            "TIDAL_SQL__AUTH": "entra_mi",
            "TIDAL_SQL__SERVER": "x.database.windows.net",
            "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
        }
    )
    async with make_client(create_app(dev)) as c:
        assert (await c.get("/docs")).status_code == 404
        assert (await c.get("/openapi.json")).status_code == 404
    async with make_client(create_app(Settings.from_env({"TIDAL_ENV": "local"}))) as c:
        assert (await c.get("/openapi.json")).status_code == 200


async def test_fake_reader_unknown_instrument_and_stream(reader: FakeReader) -> None:
    with pytest.raises(NotFound):
        await reader.read_bars("NOPE.X.Y", Interval.D1, 0, 1)
    stream = await reader.stream_bars("EIA.WTI.SPOT", Interval.D1, 0, 2**62)
    assert stream.tier is Tier.COLD
    batches = [b async for b in stream.batches]
    assert len(batches) == 3
    assert batches[1].schema.equals(BAR_V1)
    assert batches[1].column("close")[0].as_py() == -37.63
    ticks = await reader.stream_ticks("EIA.WTI.SPOT", 0, 1)
    assert [b async for b in ticks.batches] == []
