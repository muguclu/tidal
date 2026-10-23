"""Worker consumer tests with a fake Service Bus receiver/sender and an in-process reader."""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema_rs
import pytest
from azure.servicebus import TransportType
from pydantic import SecretStr
from typer.testing import CliRunner

from fakes.reader import FakeReader
from tidal.cli import build_app
from tidal.common.timeutil import FixedClock
from tidal.contracts.messages import (
    MAX_MESSAGE_BYTES,
    QUEUE_BACKTEST_RESULTS,
    BacktestJobMessage,
    BacktestResultMessage,
)
from tidal.storage.parquet_writer import LocalStore
from tidal.storage.tiering import TableResult
from tidal.worker import dlq
from tidal.worker.consumer import (
    MessageTooLarge,
    Outbound,
    Verdict,
    Worker,
    serialise,
    validation_summary,
)
from tidal.worker.handlers import BacktestHandler, TieringHandler
from tidal.worker.main import run_worker

JOB = "bt_01HZX3Y7K9QWE5TY8VPAS3DFGH"
PREFIX = f"backtests/{JOB}/"
DAY = 86_400 * 1_000_000_000
START = int(datetime(2020, 4, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
END = START + 60 * DAY


@dataclass
class Msg:
    payload: bytes
    message_id: str = "m1"
    correlation_id: str | None = "corr-1"
    delivery_count: int | None = 0  # azure: prior failed attempts

    @property
    def body(self) -> Iterator[bytes]:
        yield self.payload


@dataclass
class FakeReceiver:
    inbox: list[Msg] = field(default_factory=list)
    settled: list[tuple[str, Any]] = field(default_factory=list)
    renewals: int = 0
    renew_error: Exception | None = None

    def receive_messages(
        self, max_message_count: int | None = 1, max_wait_time: float | None = 0
    ) -> list[Msg]:
        n = max_message_count or 1
        out, self.inbox = self.inbox[:n], self.inbox[n:]
        return out

    def complete_message(self, message: Any) -> None:
        self.settled.append(("complete", None))

    def abandon_message(self, message: Any) -> None:
        self.settled.append(("abandon", None))

    def dead_letter_message(
        self, message: Any, reason: str | None = None, error_description: str | None = None
    ) -> None:
        self.settled.append(("dlq", reason))

    def renew_message_lock(self, message: Any) -> object:
        if self.renew_error:
            raise self.renew_error
        self.renewals += 1
        return None


@dataclass
class FakeSender:
    sent: list[Any] = field(default_factory=list)

    def send_messages(self, message: Any) -> None:
        self.sent.append(message)


def envelope(schema: str, payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "schema": schema,
            "message_id": "m1",
            "correlation_id": "corr-1",
            "produced_at": "2026-10-21T10:00:00Z",
            "producer": "tidal-api@1.0.0",
            "payload": payload,
        }
    ).encode()


def job_payload(instrument: str = "EIA.BRENT.SPOT") -> dict[str, Any]:
    return {
        "job_id": JOB,
        "request": {
            "strategy": "ma_crossover",
            "instrument_ids": [instrument],
            "interval": "1d",
            "start": START,
            "end": END,
            "costs": {"fee_bps": 1.0, "slippage_bps": 1.0},
            "seed": 1,
            "params": {"fast": 1, "slow": 2},
        },
        "output_prefix": PREFIX,
    }


def make(tmp_path: Path, **kw: Any) -> tuple[Worker, FakeReceiver, FakeSender, LocalStore]:
    store = LocalStore(tmp_path)
    handler = BacktestHandler(store, FixedClock(START), reader=FakeReader())
    rx, tx = FakeReceiver(), FakeSender()
    worker = Worker(rx, handler, {QUEUE_BACKTEST_RESULTS: tx}, **kw)
    return worker, rx, tx, store


def result_of(sender: FakeSender, i: int = 0) -> BacktestResultMessage:
    return BacktestResultMessage.model_validate_json(b"".join(sender.sent[i].body))


def test_backtest_success_writes_outputs_and_sends_result(tmp_path: Path) -> None:
    worker, rx, tx, store = make(tmp_path)
    msg = Msg(envelope("backtest.job/v1", job_payload()))
    assert worker.process(msg) is Verdict.COMPLETED
    assert [s[0] for s in rx.settled] == ["complete"]
    for name in ("equity.parquet", "trades.parquet", "metrics.json", "_SUCCESS"):
        assert store.exists(PREFIX + name)
    res = result_of(tx).payload
    assert res.status == "succeeded"
    assert res.result_sha256 == store.read(PREFIX + "_SUCCESS").decode()
    sent = tx.sent[0]
    assert sent.message_id == f"{JOB}.result"
    assert sent.application_properties["schema"] == "backtest.result/v1"


def test_redelivery_is_idempotent(tmp_path: Path) -> None:
    worker, _, tx, store = make(tmp_path)
    worker.process(Msg(envelope("backtest.job/v1", job_payload())))
    before = {p: store.read(p) for p in store.list("backtests/")}
    worker.process(Msg(envelope("backtest.job/v1", job_payload()), delivery_count=1))
    assert {p: store.read(p) for p in store.list("backtests/")} == before  # nothing rewritten
    first, second = result_of(tx, 0).payload, result_of(tx, 1).payload
    assert (first.result_sha256, first.metrics) == (second.result_sha256, second.metrics)
    assert second.attempt == 2


def test_unknown_instrument_is_failed_result_not_retry(tmp_path: Path) -> None:
    worker, _, tx, _ = make(tmp_path)
    msg = Msg(envelope("backtest.job/v1", job_payload("EIA.GHOST.SPOT")))
    assert worker.process(msg) is Verdict.COMPLETED
    res = result_of(tx).payload
    assert res.status == "failed"
    assert res.error is not None
    assert res.error.code == "NOT_FOUND"


def test_data_gap_is_failed_result(tmp_path: Path) -> None:
    worker, _, tx, _ = make(tmp_path)
    p = job_payload()
    p["request"]["start"] = START + 1000 * DAY
    p["request"]["end"] = START + 1010 * DAY
    p["output_prefix"] = PREFIX
    assert worker.process(Msg(envelope("backtest.job/v1", p))) is Verdict.COMPLETED
    err = result_of(tx).payload.error
    assert err is not None
    assert err.code == "DATA_GAP"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "invalid-json"),
        (envelope("tiering.job/v1", {}), "unknown-schema"),
        (envelope("backtest.job/v1", {"job_id": "x"}), "invalid-message"),
        (b" " * (MAX_MESSAGE_BYTES + 1), "message-too-large"),
    ],
    ids=["json", "schema", "invalid", "toolarge"],
)
def test_poison_messages_dead_lettered_with_reason(
    tmp_path: Path, body: bytes, reason: str
) -> None:
    worker, rx, tx, _ = make(tmp_path)
    assert worker.process(Msg(body)) is Verdict.DEAD_LETTERED
    assert rx.settled == [("dlq", reason)]
    assert tx.sent == []


class Boom:
    schema = "backtest.job/v1"

    def __init__(self, inner: BacktestHandler) -> None:
        self.inner = inner

    def parse(self, body: bytes) -> object:
        return self.inner.parse(body)

    def execute(self, message: object, attempt: int, correlation_id: str) -> list[Outbound]:
        raise ConnectionError("down")

    def on_exhausted(
        self, message: object, attempt: int, correlation_id: str, error: BaseException
    ) -> list[Outbound]:
        return self.inner.on_exhausted(message, attempt, correlation_id, error)


def test_transient_error_abandons_then_last_attempt_dead_letters(tmp_path: Path) -> None:
    worker, rx, tx, store = make(tmp_path)
    assert isinstance(worker.handler, BacktestHandler)
    worker.handler = Boom(worker.handler)
    body = envelope("backtest.job/v1", job_payload())
    assert worker.process(Msg(body, delivery_count=0)) is Verdict.ABANDONED
    assert worker.process(Msg(body, delivery_count=2)) is Verdict.DEAD_LETTERED
    assert [s[0] for s in rx.settled] == ["abandon", "dlq"]
    failed = result_of(tx).payload  # the aggregator is told on the last attempt
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.code == "INTERNAL"
    assert not store.exists(PREFIX + "_SUCCESS")


class Slow:
    """Handler that blocks until released, to exercise renewal and SIGTERM."""

    schema = "backtest.job/v1"

    def __init__(self, inner: BacktestHandler) -> None:
        self.inner = inner
        self.release = threading.Event()

    def parse(self, body: bytes) -> object:
        return self.inner.parse(body)

    def execute(self, message: object, attempt: int, correlation_id: str) -> list[Outbound]:
        self.release.wait(10)
        return []

    def on_exhausted(self, *a: Any) -> list[Outbound]:
        return []


def test_lock_is_renewed_while_job_runs(tmp_path: Path) -> None:
    worker, rx, _, _ = make(tmp_path)
    assert isinstance(worker.handler, BacktestHandler)
    slow = Slow(worker.handler)
    worker.handler = slow
    ticks = 0

    def wait(ev: threading.Event, t: float) -> bool:
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            slow.release.set()
        return False

    worker.wait = wait
    verdict = worker.process(Msg(envelope("backtest.job/v1", job_payload())))
    assert verdict is Verdict.COMPLETED
    assert rx.renewals >= 2


def test_sigterm_abandons_in_flight_message(tmp_path: Path) -> None:
    worker, rx, _, _ = make(tmp_path)
    assert isinstance(worker.handler, BacktestHandler)
    slow = Slow(worker.handler)
    worker.handler = slow

    def stop_now(ev: threading.Event, t: float) -> bool:
        worker.request_stop()
        return True

    worker.wait = stop_now
    rx.inbox.append(Msg(envelope("backtest.job/v1", job_payload())))
    code = worker.run(max_messages=0, max_wait_s=0)
    slow.release.set()
    assert code == 143
    assert rx.settled == [("abandon", None)]


def test_lock_lost_during_renewal_does_not_settle(tmp_path: Path) -> None:
    worker, rx, tx, _ = make(tmp_path)
    assert isinstance(worker.handler, BacktestHandler)
    slow = Slow(worker.handler)
    worker.handler = slow
    rx.renew_error = RuntimeError("lock lost")
    worker.wait = lambda ev, t: False
    assert worker.process(Msg(envelope("backtest.job/v1", job_payload()))) is Verdict.LOCK_LOST
    slow.release.set()
    assert rx.settled == []
    assert tx.sent == []


def test_run_stops_when_queue_empty(tmp_path: Path) -> None:
    worker, rx, _, _ = make(tmp_path)
    assert worker.run(max_messages=5, max_wait_s=0) == 0
    assert rx.settled == []


def test_serialise_drops_none_but_keeps_nullable_metrics_and_caps_size(tmp_path: Path) -> None:
    worker, _, tx, _ = make(tmp_path)
    worker.process(Msg(envelope("backtest.job/v1", job_payload())))
    data = json.loads(b"".join(tx.sent[0].body))
    assert "error" not in data["payload"]
    assert "parent_job_id" not in data["payload"]
    assert set(data["payload"]["metrics"]) >= {"cagr", "hit_rate", "n_trades"}
    big = result_of(tx)
    object.__setattr__(big.payload, "node_pool", "x" * MAX_MESSAGE_BYTES)
    with pytest.raises(MessageTooLarge):
        serialise(big)


# -- tiering -----------------------------------------------------------------------------------
def tiering_worker(
    monkeypatch: pytest.MonkeyPatch, result: TableResult
) -> tuple[Worker, FakeReceiver]:
    calls: list[Any] = []

    def fake_run_job(*args: Any) -> TableResult:
        calls.append(args[-1])
        return result

    monkeypatch.setattr("tidal.worker.handlers.run_job", fake_run_job)
    handler = TieringHandler(None, None, None, FixedClock(START))  # type: ignore[arg-type]
    rx = FakeReceiver()
    worker = Worker(rx, handler, {})
    worker.calls = calls  # type: ignore[attr-defined]
    return worker, rx


TIERING = envelope(
    "tiering.job/v1",
    {"table": "md.bar_hot", "hot_window_days": 90, "dry_run": False, "batch_rows": 1000},
)


def test_tiering_job_runs_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, rx = tiering_worker(monkeypatch, TableResult(table="md.bar_hot"))
    assert worker.process(Msg(TIERING)) is Verdict.COMPLETED
    assert len(worker.calls) == 1  # type: ignore[attr-defined]
    assert rx.settled == [("complete", None)]


def test_tiering_locked_is_normal_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, rx = tiering_worker(monkeypatch, TableResult(table="md.bar_hot", locked=True))
    assert worker.process(Msg(TIERING)) is Verdict.COMPLETED
    assert rx.settled == [("complete", None)]


def test_tiering_errors_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, _ = tiering_worker(monkeypatch, TableResult(table="md.bar_hot", errors=["boom"]))
    assert worker.process(Msg(TIERING)) is Verdict.ABANDONED


def test_tiering_invalid_payload_dead_letters(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, rx = tiering_worker(monkeypatch, TableResult(table="md.bar_hot"))
    bad = envelope("tiering.job/v1", {"table": "md.other", "hot_window_days": 0})
    assert worker.process(Msg(bad)) is Verdict.DEAD_LETTERED
    assert rx.settled == [("dlq", "invalid-message")]


# -- dlq CLI -----------------------------------------------------------------------------------
class DlqMsg:
    def __init__(self, mid: str) -> None:
        self.message_id = mid
        self.correlation_id = "c"
        self.content_type = "application/json"
        self.application_properties = {b"schema": b"backtest.job/v1"}
        self.dead_letter_reason = "invalid-message"
        self.dead_letter_error_description = "payload.job_id: bad"
        self.delivery_count = 3
        self.body = [b'{"a":', b"1}"]


class DlqEndpoint:
    def __init__(self, msgs: list[DlqMsg]) -> None:
        self.msgs = msgs
        self.completed: list[str] = []
        self.abandoned: list[str] = []
        self.sent: list[Any] = []

    def __enter__(self) -> "DlqEndpoint":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def peek_messages(self, max_message_count: int) -> list[DlqMsg]:
        return self.msgs[:max_message_count]

    def receive_messages(self, max_message_count: int, max_wait_time: float) -> list[DlqMsg]:
        return self.msgs[:max_message_count]

    def complete_message(self, m: DlqMsg) -> None:
        self.completed.append(m.message_id)

    def abandon_message(self, m: DlqMsg) -> None:
        self.abandoned.append(m.message_id)

    def send_messages(self, m: Any) -> None:
        self.sent.append(m)


class DlqClient:
    def __init__(self, ep: DlqEndpoint) -> None:
        self.ep = ep
        self.sub_queues: list[Any] = []

    def get_queue_receiver(self, queue_name: str, **kw: Any) -> DlqEndpoint:
        self.sub_queues.append(kw.get("sub_queue"))
        return self.ep

    def get_queue_sender(self, queue_name: str, **kw: Any) -> DlqEndpoint:
        return self.ep


def test_dlq_inspect_lists_without_settling() -> None:
    ep = DlqEndpoint([DlqMsg("a"), DlqMsg("b")])
    entries = dlq.inspect(DlqClient(ep), "backtest-jobs")
    assert [e.message_id for e in entries] == ["a", "b"]
    assert entries[0].reason == "invalid-message"
    assert entries[0].schema == "backtest.job/v1"
    assert entries[0].body_bytes == 7
    assert ep.completed == []


def test_dlq_resubmit_copies_then_completes_and_filters() -> None:
    ep = DlqEndpoint([DlqMsg("a"), DlqMsg("b")])
    assert dlq.resubmit(DlqClient(ep), "backtest-jobs", message_id="b") == 1
    assert ep.completed == ["b"]
    assert ep.abandoned == ["a"]
    assert b"".join(ep.sent[0].body) == b'{"a":1}'


def test_dlq_cli_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = DlqEndpoint([DlqMsg("a")])
    monkeypatch.setattr(dlq, "_client", lambda: DlqClient(ep))
    runner = CliRunner()
    out = runner.invoke(dlq.app, ["inspect", "backtest-jobs"])
    assert out.exit_code == 0
    assert "reason=invalid-message" in out.output
    out = runner.invoke(dlq.app, ["resubmit", "backtest-jobs"])
    assert out.exit_code == 0
    assert "resubmitted 1" in out.output


def test_dlq_registered_in_root_cli() -> None:
    result = CliRunner().invoke(build_app(), ["dlq", "--help"])
    assert result.exit_code == 0
    assert "resubmit" in result.output


def test_websocket_transport_off_local(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Fake:
        @staticmethod
        def from_connection_string(conn: str, **kw: Any) -> str:
            seen.update(kw)
            return "client"

    monkeypatch.setattr("azure.servicebus.ServiceBusClient", Fake)
    for env, expected in (("dev", TransportType.AmqpOverWebsocket), ("local", TransportType.Amqp)):
        settings = SimpleNamespace(
            env=env,
            servicebus=SimpleNamespace(connection_string=SecretStr("Endpoint=sb://x;")),
        )
        dlq.client_from_settings(settings)  # type: ignore[arg-type]
        assert seen["transport_type"] == expected


# -- review round ------------------------------------------------------------------------------
def test_third_real_delivery_reports_failed_and_dead_letters(tmp_path: Path) -> None:
    worker, rx, tx, _ = make(tmp_path)
    assert isinstance(worker.handler, BacktestHandler)
    worker.handler = Boom(worker.handler)
    body = envelope("backtest.job/v1", job_payload())
    assert worker.process(Msg(body, delivery_count=2)) is Verdict.DEAD_LETTERED
    res = result_of(tx).payload
    assert res.status == "failed"
    assert res.attempt == 3
    assert res.error is not None
    assert res.error.code == "INTERNAL"
    assert rx.settled == [("dlq", "max-delivery-exceeded")]


def test_message_received_after_sigterm_is_abandoned_without_running(tmp_path: Path) -> None:
    worker, rx, tx, store = make(tmp_path)
    worker.request_stop()
    msg = Msg(envelope("backtest.job/v1", job_payload()))
    assert worker.process(msg) is Verdict.ABANDONED
    assert rx.settled == [("abandon", None)]
    assert not store.exists(PREFIX + "_SUCCESS")
    assert tx.sent == []


def test_orphaned_job_thread_is_joined_before_next_receive(tmp_path: Path) -> None:
    worker, rx, _, _ = make(tmp_path)
    assert isinstance(worker.handler, BacktestHandler)
    slow = Slow(worker.handler)
    worker.handler = slow
    rx.renew_error = RuntimeError("lock lost")
    worker.wait = lambda ev, t: False
    worker.orphan_join_s = 0.01
    assert worker.process(Msg(envelope("backtest.job/v1", job_payload()))) is Verdict.LOCK_LOST
    assert len(worker._orphans) == 1
    worker.run(max_messages=1, max_wait_s=0)
    assert len(worker._orphans) == 1  # still running after the bounded join: recorded, not lost
    slow.release.set()
    worker._orphans[0].join(5)
    worker._join_orphans()
    assert worker._orphans == []


class RacingStore(LocalStore):
    """Reports every path as absent, so put_new meets files another worker already wrote."""

    def exists(self, path: str) -> bool:
        return False


def test_same_job_id_race_treats_existing_outputs_as_written(tmp_path: Path) -> None:
    worker, _, tx, store = make(tmp_path)
    worker.process(Msg(envelope("backtest.job/v1", job_payload())))  # the "other" worker
    racer = BacktestHandler(RacingStore(tmp_path), FixedClock(START), reader=FakeReader())
    rx2 = FakeReceiver()
    w2 = Worker(rx2, racer, {QUEUE_BACKTEST_RESULTS: tx})
    assert w2.process(Msg(envelope("backtest.job/v1", job_payload()))) is Verdict.COMPLETED
    assert rx2.settled == [("complete", None)]
    assert result_of(tx, 1).payload.result_sha256 == store.read(PREFIX + "_SUCCESS").decode()


def test_dlq_description_never_contains_input_values_or_messages() -> None:
    sentinel = "SENTINELVALUE"
    p = job_payload()
    p["output_prefix"] = f"backtests/{sentinel}/"  # model validator raises ValueError(msg)
    p["request"]["instrument_ids"] = [sentinel]
    try:
        BacktestJobMessage.model_validate_json(envelope("backtest.job/v1", p))
    except Exception as exc:
        text = validation_summary(exc)  # type: ignore[arg-type]
    assert sentinel not in text
    assert "output_prefix" in text or "instrument_ids" in text


def test_result_body_validates_against_json_schema_with_null_hit_rate(tmp_path: Path) -> None:
    schema = json.loads(Path("docs/contracts/schemas/backtest.result.v1.json").read_text("utf-8"))
    worker, _, tx, _ = make(tmp_path)
    worker.process(Msg(envelope("backtest.job/v1", job_payload())))
    body = json.loads(b"".join(tx.sent[0].body))
    jsonschema_rs.validator_for(schema).validate(body)
    assert "hit_rate" in body["payload"]["metrics"]
    assert body["payload"]["metrics"]["hit_rate"] is None
    assert "error" not in body["payload"]


def test_tiering_queue_opens_no_results_sender() -> None:
    opened: list[str] = []

    class Ep:
        def __enter__(self) -> "Ep":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def receive_messages(self, **kw: Any) -> list[Any]:
            return []

    class Client:
        def get_queue_receiver(self, queue_name: str, **kw: Any) -> Any:
            opened.append(f"rx:{queue_name}")
            return Ep()

        def get_queue_sender(self, queue_name: str, **kw: Any) -> Any:
            opened.append(f"tx:{queue_name}")
            return Ep()

    handler = TieringHandler(None, None, None, FixedClock(START))  # type: ignore[arg-type]
    assert run_worker(Client(), handler, "tiering-jobs", 1, 0) == 0
    assert opened == ["rx:tiering-jobs"]
    run_worker(Client(), BacktestHandler(None, FixedClock(START)), "backtest-jobs", 1, 0)  # type: ignore[arg-type]
    assert "tx:backtest-results" in opened
