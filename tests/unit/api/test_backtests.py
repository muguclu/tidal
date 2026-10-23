"""Backtest submit/status/list endpoints and the result aggregator (T-304) on fakes."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import tidal.api.routers.backtests as bt
from fakes.backtest_store import FakeBacktestStore
from fakes.bus import FakeBus
from tidal.api.aggregator import ResultAggregator
from tidal.api.app import create_app
from tidal.api.deps import ALL_ROLES, Principal, get_principal
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.messages import BacktestJobMessage
from tidal.contracts.models import JobStatus
from tidal.worker.consumer import Verdict, serialise

pytestmark = pytest.mark.anyio
PROBLEM = "application/problem+json"
NOW_NS = 1_700_000_000 * 10**9
SHA = "a" * 64
METRICS = {"cagr": 0.1, "ann_vol": 0.2, "sharpe": 0.5, "max_drawdown": -0.1, "n_trades": 3}
JSON_CT = {"content-type": "application/json"}
Json = dict[str, Any]  # Any: JSON documents


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def request_body(**over: Any) -> Json:  # Any: JSON values
    body: Json = {
        "strategy": "ma_crossover",
        "instrument_ids": ["EIA.WTI.SPOT"],
        "params": {"fast": 5, "slow": 20},
        "interval": "1d",
        "start": "2020-01-01T00:00:00Z",
        "end": "2021-01-01T00:00:00Z",
        "costs": {"fee_bps": 1, "slippage_bps": 1},
        "seed": 1,
    }
    return body | over


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def store() -> FakeBacktestStore:
    return FakeBacktestStore()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW_NS)


@pytest.fixture
def app(bus: FakeBus, store: FakeBacktestStore, clock: FixedClock) -> FastAPI:
    settings = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
    application = create_app(settings, bus=bus, clock=clock)
    application.state.backtest_store = store
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def as_principal(app: FastAPI, oid: str, *roles: str) -> None:
    app.dependency_overrides[get_principal] = lambda: Principal(oid, frozenset(roles))


def jobs_of(bus: FakeBus) -> list[BacktestJobMessage]:
    return [m for m in bus.sent if isinstance(m, BacktestJobMessage)]


# --- submit ------------------------------------------------------------------------------------


async def test_submit_single_job(
    client: httpx.AsyncClient, bus: FakeBus, store: FakeBacktestStore
) -> None:
    r = await client.post("/v1/backtests", json=request_body())
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "queued"
    (msg,) = jobs_of(bus)
    assert msg.payload.job_id == job_id
    assert msg.payload.parent_job_id is None
    assert msg.payload.output_prefix == f"backtests/{job_id}/"
    assert msg.message_id == job_id
    assert set(store.rows) == {job_id}
    assert len(store.rows[job_id].owner_hash) == 64  # hashed, never the raw oid


async def test_sweep_fans_out_children(
    client: httpx.AsyncClient, bus: FakeBus, store: FakeBacktestStore
) -> None:
    r = await client.post(
        "/v1/backtests", json=request_body(sweep={"fast": [3, 5], "slow": [10, 20, 30]})
    )
    assert r.status_code == 202
    parent = r.json()["job_id"]
    jobs = [m.payload for m in jobs_of(bus)]
    assert len(jobs) == 6
    assert store.rows[parent].children_total == 6
    assert [j.child_index for j in jobs] == list(range(6))
    assert all(j.parent_job_id == parent and j.request.sweep is None for j in jobs)
    combos = {(j.request.params.fast, j.request.params.slow) for j in jobs}  # type: ignore[union-attr]
    assert combos == {(f, s) for f in (3, 5) for s in (10, 20, 30)}
    assert len({j.job_id for j in jobs}) == 6
    assert len(store.children(parent)) == 6


async def test_sweep_batches_respect_message_count(
    client: httpx.AsyncClient, bus: FakeBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bt, "BATCH_MAX_MESSAGES", 4)
    r = await client.post("/v1/backtests", json=request_body(sweep={"slow": list(range(10, 20))}))
    assert r.status_code == 202
    assert [len(b) for b in bus.batches] == [4, 4, 2]


async def test_sweep_batches_respect_byte_budget(
    client: httpx.AsyncClient, bus: FakeBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bt, "BATCH_MAX_BYTES", 2_000)
    r = await client.post("/v1/backtests", json=request_body(sweep={"slow": list(range(10, 20))}))
    assert r.status_code == 202
    assert len(bus.batches) > 1
    for batch in bus.batches:
        assert sum(len(serialise(m)) for m in batch) <= 2_000
    assert sum(len(b) for b in bus.batches) == 10


async def test_sweep_over_200_is_413(
    client: httpx.AsyncClient, bus: FakeBus, store: FakeBacktestStore
) -> None:
    sweep = {"fast": list(range(1, 21)), "slow": list(range(30, 42))}  # 240 children
    r = await client.post("/v1/backtests", json=request_body(sweep=sweep))
    assert r.status_code == 413
    assert r.headers["content-type"].startswith(PROBLEM)
    assert r.json()["correlation_id"]
    assert not bus.sent
    assert not store.rows


async def test_invalid_request_is_400(client: httpx.AsyncClient, bus: FakeBus) -> None:
    for bad in (
        request_body(instrument_ids=["A.B.C", "D.E.F"]),
        request_body(interval="1s"),
        request_body(sweep={"nope": [1]}),
        request_body(start="2021-01-01T00:00:00Z", end="2020-01-01T00:00:00Z"),
    ):
        r = await client.post("/v1/backtests", json=bad)
        assert r.status_code == 400, bad
    r = await client.post("/v1/backtests", content=b"{not json", headers=JSON_CT)
    assert r.status_code == 400
    assert not bus.sent


async def test_message_over_limit_is_413(
    client: httpx.AsyncClient, bus: FakeBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tidal.worker.consumer.MAX_MESSAGE_BYTES", 100)
    r = await client.post("/v1/backtests", json=request_body())
    assert r.status_code == 413
    assert not bus.sent


async def test_bus_failure_marks_unsent_failed_and_is_503(
    client: httpx.AsyncClient,
    bus: FakeBus,
    store: FakeBacktestStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bt, "BATCH_MAX_MESSAGES", 2)
    real = bus.send_batch
    calls = 0

    async def flaky(messages: Any) -> int:  # Any: Sequence[OutboundMessage]
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("down")
        return await real(messages)

    bus.send_batch = flaky  # type: ignore[method-assign]
    r = await client.post("/v1/backtests", json=request_body(sweep={"slow": [10, 11, 12, 13]}))
    assert r.status_code == 503
    assert r.headers["content-type"].startswith(PROBLEM)
    parent = next(k for k, v in store.rows.items() if v.children_total)
    statuses = [c.status for c in store.children(parent)]
    assert statuses == [JobStatus.QUEUED] * 2 + [JobStatus.FAILED] * 2
    assert store.rows[parent].status == JobStatus.RUNNING


async def test_roles_and_missing_bus(app: FastAPI, client: httpx.AsyncClient) -> None:
    as_principal(app, "u", "Tidal.Trader")
    assert (await client.post("/v1/backtests", json=request_body())).status_code == 403
    assert (await client.get("/v1/backtests")).status_code == 403
    as_principal(app, "u", "Tidal.Quant")
    app.state.bus = None
    assert (await client.post("/v1/backtests", json=request_body())).status_code == 503


# --- aggregator --------------------------------------------------------------------------------


def result_msg(job: BacktestJobMessage, status: str = "succeeded", **kw: Any) -> Json:
    payload: Json = {
        "job_id": job.payload.job_id,
        "parent_job_id": job.payload.parent_job_id,
        "status": status,
        "duration_ms": 5,
        "node_pool": "spot",
        "attempt": 1,
    }
    if status == "succeeded":
        payload |= {"metrics": dict(METRICS), "result_sha256": SHA}  # hit_rate/turnover omitted
    else:
        payload |= {"error": {"code": "DATA_GAP", "detail": "x"}}
    return {
        "schema": "backtest.result/v1",
        "message_id": f"{job.payload.job_id}.result",
        "correlation_id": "c1",
        "produced_at": "2023-11-14T22:13:20Z",
        "producer": "tidal-worker@1",
        "payload": payload | kw,
    }


class Msg:
    def __init__(self, body: Json | bytes, delivery: int = 0) -> None:
        self.body = [body if isinstance(body, bytes) else json.dumps(body).encode()]
        self.message_id = "m"
        self.correlation_id = "c1"
        self.delivery_count = delivery


class Receiver:
    def __init__(self) -> None:
        self.settled: list[tuple[str, str | None]] = []
        self.fail_settle = False

    def receive_messages(
        self, max_message_count: int | None = None, max_wait_time: float | None = None
    ) -> list[Any]:  # Any: broker messages
        return []

    def _record(self, kind: str, reason: str | None = None) -> None:
        if self.fail_settle:
            raise RuntimeError("lock lost")
        self.settled.append((kind, reason))

    def complete_message(self, message: Any) -> None:  # Any: broker message
        self._record("complete")

    def abandon_message(self, message: Any) -> None:  # Any: broker message
        self._record("abandon")

    def dead_letter_message(
        self,
        message: Any,  # Any: broker message
        reason: str | None = None,
        error_description: str | None = None,
    ) -> None:
        self._record("dead_letter", reason)

    def renew_message_lock(self, message: Any) -> object:  # Any: broker message
        return None


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture
def agg(store: FakeBacktestStore, clock: FixedClock, receiver: Receiver) -> ResultAggregator:
    return ResultAggregator(store, clock, receiver)


async def apply(agg: ResultAggregator, body: Json | bytes) -> Verdict:
    return await agg.process(Msg(body))


async def test_single_job_lifecycle(
    client: httpx.AsyncClient, bus: FakeBus, agg: ResultAggregator
) -> None:
    job_id = (await client.post("/v1/backtests", json=request_body())).json()["job_id"]
    r = await client.get(f"/v1/backtests/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert r.json()["result"] is None
    assert await apply(agg, result_msg(jobs_of(bus)[0])) == Verdict.COMPLETED
    body = (await client.get(f"/v1/backtests/{job_id}")).json()
    assert body["status"] == "succeeded"
    assert body["finished_at"] is not None
    assert body["children"] == {"total": 1, "succeeded": 1, "failed": 0}
    assert body["result"]["result_sha256"] == SHA
    assert body["result"]["metrics"]["hit_rate"] is None  # restored nullable metric
    assert body["result"]["equity_url"] == f"backtests/{job_id}/equity.parquet"


async def test_sweep_parent_status_and_idempotent_redelivery(
    client: httpx.AsyncClient, bus: FakeBus, agg: ResultAggregator, store: FakeBacktestStore
) -> None:
    r = await client.post("/v1/backtests", json=request_body(sweep={"slow": [10, 20, 30]}))
    parent = r.json()["job_id"]
    jobs = jobs_of(bus)
    await apply(agg, result_msg(jobs[0]))
    await apply(agg, result_msg(jobs[0]))  # redelivery
    body = (await client.get(f"/v1/backtests/{parent}")).json()
    assert body["status"] == "running"
    assert body["children"] == {"total": 3, "succeeded": 1, "failed": 0}
    await apply(agg, result_msg(jobs[0], "failed"))  # first terminal result wins
    assert (await client.get(f"/v1/backtests/{parent}")).json()["children"]["failed"] == 0
    await apply(agg, result_msg(jobs[1]))
    await apply(agg, result_msg(jobs[2], "failed"))
    body = (await client.get(f"/v1/backtests/{parent}")).json()
    assert body["status"] == "partial"
    assert body["result"] is None
    assert body["finished_at"] is not None
    assert store.rows[parent].status == JobStatus.PARTIAL


async def test_all_failed_parent(
    client: httpx.AsyncClient, bus: FakeBus, agg: ResultAggregator
) -> None:
    r = await client.post("/v1/backtests", json=request_body(sweep={"slow": [10, 20]}))
    for m in jobs_of(bus):
        await apply(agg, result_msg(m, "failed"))
    assert (await client.get(f"/v1/backtests/{r.json()['job_id']}")).json()["status"] == "failed"


async def test_lost_parent_update_heals_on_redelivery(
    client: httpx.AsyncClient, bus: FakeBus, agg: ResultAggregator, store: FakeBacktestStore
) -> None:
    r = await client.post("/v1/backtests", json=request_body(sweep={"slow": [10]}))
    parent = r.json()["job_id"]
    job = jobs_of(bus)[0]
    await store.finish_child(  # child applied, parent update crashed
        job.payload.job_id,
        status=JobStatus.SUCCEEDED,
        metrics_json=None,
        result_sha256=None,
        error_code=None,
        finished_at=agg.clock.now(),
    )
    assert store.rows[parent].status == JobStatus.QUEUED
    await apply(agg, result_msg(job))
    assert store.rows[parent].status == JobStatus.SUCCEEDED


async def test_aggregator_settlement(
    client: httpx.AsyncClient,
    bus: FakeBus,
    agg: ResultAggregator,
    receiver: Receiver,
    store: FakeBacktestStore,
) -> None:
    await client.post("/v1/backtests", json=request_body())
    job = jobs_of(bus)[0]
    good = result_msg(job)
    cases: list[tuple[Json | bytes, str, str | None]] = [
        (b"not json", "dead_letter", "invalid-json"),
        (good | {"schema": "backtest.job/v1"}, "dead_letter", "unknown-schema"),
        (result_msg(job, result_sha256=None), "dead_letter", "invalid-message"),
        (result_msg(job, job_id="bt_01J00000000000000000000000"), "dead_letter", "unknown-job"),
        (
            result_msg(job, parent_job_id="bt_01J00000000000000000000001"),
            "dead_letter",
            "unknown-job",
        ),
        (good, "complete", None),
    ]
    for body, kind, reason in cases:
        await apply(agg, body)
        assert receiver.settled[-1] == (kind, reason)
    store.fail_next = True  # transient store error: abandon, not dead-letter
    assert await apply(agg, good) == Verdict.ABANDONED
    receiver.fail_settle = True
    assert await apply(agg, good) == Verdict.LOCK_LOST


# --- status / list -----------------------------------------------------------------------------


async def test_ownership_and_children_are_404(
    app: FastAPI, client: httpx.AsyncClient, bus: FakeBus
) -> None:
    as_principal(app, "alice", "Tidal.Quant")
    r = await client.post("/v1/backtests", json=request_body(sweep={"slow": [10, 20]}))
    parent = r.json()["job_id"]
    child = jobs_of(bus)[0].payload.job_id
    assert (await client.get(f"/v1/backtests/{child}")).status_code == 404
    as_principal(app, "bob", "Tidal.Quant")
    r = await client.get(f"/v1/backtests/{parent}")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM)
    assert (await client.get("/v1/backtests/not-an-id")).status_code == 400
    assert (await client.get("/v1/backtests")).json() == {"items": [], "next_cursor": None}


async def test_list_paging_and_filter(
    app: FastAPI, client: httpx.AsyncClient, clock: FixedClock
) -> None:
    as_principal(app, "alice", *ALL_ROLES)
    ids = []
    for _ in range(3):
        clock.advance(10**9)
        ids.append((await client.post("/v1/backtests", json=request_body())).json()["job_id"])
    page1 = (await client.get("/v1/backtests", params={"limit": 2})).json()
    assert [i["job_id"] for i in page1["items"]] == ids[::-1][:2]
    assert all(i["result"] is None for i in page1["items"])
    params = {"limit": 2, "cursor": page1["next_cursor"]}
    page2 = (await client.get("/v1/backtests", params=params)).json()
    assert [i["job_id"] for i in page2["items"]] == [ids[0]]
    assert page2["next_cursor"] is None
    assert (await client.get("/v1/backtests", params={"cursor": "garbage"})).status_code == 400
    # a cursor is bound to the status filter
    params = {"cursor": page1["next_cursor"], "status": "failed"}
    assert (await client.get("/v1/backtests", params=params)).status_code == 400
    r = await client.get("/v1/backtests", params={"status": "succeeded"})
    assert r.json()["items"] == []
