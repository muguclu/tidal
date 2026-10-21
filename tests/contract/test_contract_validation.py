"""Validation rules of the contract models (data-contracts, api-contract, messaging-contract)."""

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tidal.contracts.instruments import CATALOGUE_V1, INSTRUMENT_ID_RE, Instrument
from tidal.contracts.messages import (
    MESSAGE_QUEUES,
    QUEUE_BACKTEST_JOBS,
    BacktestJobMessage,
    BacktestResultMessage,
    TieringJobMessage,
)
from tidal.contracts.models import (
    INT64_MAX,
    INT64_MIN,
    MAX_SWEEP_CHILDREN,
    TIME_PARAM_MAX_LENGTH,
    TIME_PARAM_PATTERN,
    BacktestAccepted,
    BacktestList,
    BacktestMetrics,
    BacktestRequest,
    BacktestStatus,
    Bar,
    BarsResponse,
    CalendarSpreadRequest,
    HealthStatus,
    JobStatus,
    LatestResponse,
    MaCrossoverRequest,
    Problem,
    SpreadPoint,
    StorageStats,
    Tick,
    TieringRunAccepted,
    to_ts_ns,
)
from tidal.contracts.schemas import QUALITY_MAX, QualityFlag, validate_quality

TS = "2020-04-20T00:00:00Z"
TS_NS = 1_587_340_800_000_000_000
JOB_ID = "bt_01J9Z3K4M5N6P7Q8R9S0T1V2W3"


def bar(**over: Any) -> dict[str, Any]:
    base = {"ts": TS, "ts_ns": TS_NS, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
    return base | {"volume": None, "quality": 0, "version": 1} | over


def bt(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "strategy": "ma_crossover",
        "instrument_ids": ["EIA.BRENT.SPOT"],
        "interval": "1d",
        "start": "2000-01-01T00:00:00Z",
        "end": "2025-12-31T00:00:00Z",
        "params": {"fast": 20, "slow": 100},
        "costs": {"fee_bps": 1.0, "slippage_bps": 2.0},
        "seed": 42,
    }
    return base | over


ZPARAMS = {"lookback": 20, "z_entry": 2.0, "z_exit": 0.5}


# --- prices & quality -------------------------------------------------------------------------


def test_negative_prices_accepted() -> None:
    wti = Bar.model_validate(bar(open=-37.63, high=-37.63, low=-37.63, close=-37.63, quality=1))
    assert wti.close == -37.63
    assert Tick.model_validate(bar_tick(price=-37.63)).price == -37.63


def bar_tick(**over: Any) -> dict[str, Any]:
    return (
        {"ts": TS, "ts_ns": TS_NS, "price": 1.0, "size": 0.0, "side": 1, "quality": 0}
        | {"version": 1}
        | over
    )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nan_and_inf_prices_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        Bar.model_validate(bar(close=bad))
    with pytest.raises(ValidationError):
        Tick.model_validate(bar_tick(price=bad))
    with pytest.raises(ValidationError):
        Bar.model_validate(bar(volume=bad))


def test_null_volume_and_ohlc_consistency() -> None:
    assert Bar.model_validate(bar()).volume is None
    with pytest.raises(ValidationError, match="low <="):
        Bar.model_validate(bar(low=1.2))
    with pytest.raises(ValidationError):
        Tick.model_validate(bar_tick(side=3))
    with pytest.raises(ValidationError):
        Tick.model_validate(bar_tick(size=-1.0))


@pytest.mark.parametrize("bad", [32, 64, 128, 0xE0, -1, 255])
def test_reserved_quality_bits_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="reserved"):
        validate_quality(bad)
    with pytest.raises(ValidationError):
        Bar.model_validate(bar(quality=bad))


@given(st.integers(min_value=0, max_value=QUALITY_MAX))
def test_defined_quality_flags_accepted(value: int) -> None:
    assert validate_quality(value) == QualityFlag(value)
    assert Bar.model_validate(bar(quality=value)).quality == value


# --- instrument_id ----------------------------------------------------------------------------

GOOD_IDS = ["EIA.BRENT.SPOT", "SYN.POWER_DE.TICK", "FRED.EURUSD.FX", "A1.B_2.C_3"]
BAD_IDS = ["eia.brent.spot", "EIA.BRENT", "EIA-BRENT-SPOT", "EIA.BRENT.SPOT.X", "EIA..SPOT",
           "EIA_X.BRENT.SPOT", " EIA.BRENT.SPOT", "A." + "B" * 61 + ".C"]  # fmt: skip


def instrument(iid: str, **over: Any) -> dict[str, Any]:
    base = {"instrument_id": iid, "source": "EIA", "asset_class": "crude", "kind": "spot"}
    return base | {"unit": "USD/bbl", "currency": "USD", "exchange_tz": "Europe/London"} | {
        "granularity": "1d", "description": "Brent spot"
    } | over  # fmt: skip


@pytest.mark.parametrize("iid", GOOD_IDS)
def test_instrument_id_accepted(iid: str) -> None:
    assert INSTRUMENT_ID_RE.fullmatch(iid)
    assert Instrument.model_validate(instrument(iid)).instrument_id == iid


@pytest.mark.parametrize("iid", BAD_IDS)
def test_instrument_id_rejected(iid: str) -> None:
    with pytest.raises(ValidationError):
        Instrument.model_validate(instrument(iid))
    with pytest.raises(ValidationError):
        BarsResponse.model_validate(
            {"instrument_id": iid, "interval": "1d", "tier": "cold", "rows": []}
        )


def test_instrument_rules() -> None:
    with pytest.raises(ValidationError, match="roll_method"):
        Instrument.model_validate(instrument("EIA.BRENT.CONT", kind="continuous"))
    cont = Instrument.model_validate(
        instrument("EIA.BRENT.CONT", kind="continuous", roll_method="ratio")
    )
    assert cont.roll_method == "ratio"
    with pytest.raises(ValidationError):
        Instrument.model_validate(instrument("EIA.BRENT.SPOT", currency="USDX"))
    assert all(INSTRUMENT_ID_RE.fullmatch(e.instrument_id) for e in CATALOGUE_V1)


# --- BacktestRequest --------------------------------------------------------------------------


def test_backtest_request_example_parses() -> None:
    req = BacktestRequest.model_validate(bt(sweep={"fast": [10, 20, 50], "slow": [100, 200]}))
    assert isinstance(req.root, MaCrossoverRequest)
    assert req.root.start == 946_684_800_000_000_000
    assert req.root.sweep_size == 6
    assert BacktestRequest.model_validate_json(req.model_dump_json()) == req


@pytest.mark.parametrize(("fast", "slow"), [(10, 20), (200, 1), (1, 200)])
def test_sweep_product_at_limit_accepted(fast: int, slow: int) -> None:
    sweep = {"fast": list(range(1, fast + 1)), "slow": list(range(1, slow + 1))}
    assert BacktestRequest.model_validate(bt(sweep=sweep)).root.sweep_size == MAX_SWEEP_CHILDREN


@pytest.mark.parametrize(
    "sweep",
    [
        {"fast": list(range(1, 68)), "slow": [1, 2, 3]},  # 201
        {"fast": list(range(1, 202))},  # 201 values on one axis
        {},
        {"fast": []},
        {"window": [1, 2]},  # not a param of ma_crossover
        {"fast": [0, 5]},  # fast must be >= 1
        {"fast": [1.5]},  # fast is an int
        {"fast": [1], "slow": [2], "x": [3], "y": [4]},  # > 3 keys
    ],
)
def test_bad_sweeps_rejected(sweep: dict[str, list[float]]) -> None:
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(bt(sweep=sweep))


@given(st.lists(st.integers(min_value=1, max_value=20), min_size=1, max_size=3))
def test_sweep_size_is_cartesian_product(sizes: list[int]) -> None:
    keys = ["lookback", "z_entry", "z_exit"][: len(sizes)]
    sweep = {k: [float(i + 2) for i in range(n)] for k, n in zip(keys, sizes, strict=False)}
    data = bt(strategy="mean_reversion", params=ZPARAMS, sweep=sweep)
    if math.prod(sizes) <= MAX_SWEEP_CHILDREN:
        assert BacktestRequest.model_validate(data).root.sweep_size == math.prod(sizes)
    else:
        with pytest.raises(ValidationError, match="max 200"):
            BacktestRequest.model_validate(data)


def test_calendar_spread_rule() -> None:
    legs = ["EIA.WTI.FUT1", "EIA.WTI.FUT2"]
    ok = BacktestRequest.model_validate(
        bt(strategy="calendar_spread", instrument_ids=legs, params=ZPARAMS)
    )
    assert isinstance(ok.root, CalendarSpreadRequest)
    for ids in (legs[:1], [*legs, "EIA.WTI.FUT3"]):
        with pytest.raises(ValidationError):
            BacktestRequest.model_validate(
                bt(strategy="calendar_spread", instrument_ids=ids, params=ZPARAMS)
            )
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(bt(instrument_ids=legs))  # ma_crossover takes 1
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(bt(strategy="calendar_spread", instrument_ids=legs))


@pytest.mark.parametrize(
    "over",
    [
        {"strategy": "buy_and_hold"},
        {"interval": "1s"},
        {"params": {"fast": 20}},
        {"params": {"fast": 20, "slow": 100, "extra": 1}},
        {"strategy": "mean_reversion", "params": {"lookback": 1, "z_entry": 2.0, "z_exit": 0.5}},
        {"strategy": "mean_reversion", "params": {"lookback": 5, "z_entry": 0.0, "z_exit": 0.5}},
        {"start": "2025-12-31T00:00:00Z", "end": "2000-01-01T00:00:00Z"},
        {"start": "2000-01-01T00:00:00"},  # naive
        {"start": "yesterday"},
        {"costs": {"fee_bps": -1.0, "slippage_bps": 0.0}},
        {"unknown": 1},
    ],
)
def test_bad_backtest_requests_rejected(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(bt(**over))


def test_time_parameters_normalise_to_ts_ns() -> None:
    assert to_ts_ns(TS) == TS_NS
    assert to_ts_ns(str(TS_NS)) == TS_NS
    assert to_ts_ns("2020-04-20T00:00:00.000000001Z") == TS_NS + 1
    assert to_ts_ns("2020-04-20T00:00:00.5Z") == TS_NS + 500_000_000
    assert to_ts_ns("1969-12-31T23:59:59Z") == -1_000_000_000
    req = BacktestRequest.model_validate(bt(start=str(TS_NS), end=TS_NS + 1))
    assert (req.root.start, req.root.end) == (TS_NS, TS_NS + 1)


@pytest.mark.parametrize(
    "bad",
    [
        "2020-04-20T02:00:00+02:00",  # offsets other than Z are rejected
        "2020-04-20T00:00:00+00:00",
        "2020-04-20T00:00:00z",
        "2020-04-20T00:00:00",
        "2020-04-20 00:00:00Z",
        "2020-04-20",
        "2020-04-20T25:00:00Z",
        "2020-04-20T00:00:00.1234567891Z",  # finer than ns
        "-1",
        "1" * 20,
        "",
        1.5,
        True,
        datetime(2020, 4, 20, tzinfo=UTC),
    ],
)
def test_time_parameters_outside_time_param_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="ISO-8601 UTC"):
        to_ts_ns(bad)


EPOCH_NAIVE = datetime(1970, 1, 1)  # noqa: DTZ001 - naive on purpose, mirrors naive inputs
INT64_DATES = st.datetimes(  # whole range representable as int64 ns
    min_value=EPOCH_NAIVE.replace(year=1678), max_value=EPOCH_NAIVE.replace(year=2262)
)


@given(INT64_DATES)
def test_to_ts_ns_is_exact(naive: datetime) -> None:
    ns = to_ts_ns(naive.isoformat() + "Z")
    assert ns % 1000 == 0
    assert EPOCH_NAIVE + timedelta(microseconds=ns // 1000) == naive


@given(st.text(alphabet="0123456789-T:.Zz+ ", max_size=40))
def test_accepted_strings_match_time_param_pattern(text: str) -> None:
    try:
        to_ts_ns(text)
    except ValueError:
        return
    assert len(text) <= TIME_PARAM_MAX_LENGTH
    assert re.fullmatch(TIME_PARAM_PATTERN, text)


@pytest.mark.parametrize("job_id", [JOB_ID, "bt_7ZZZZZZZZZZZZZZZZZZZZZZZZZ"])
def test_job_id_accepted(job_id: str) -> None:
    assert BacktestAccepted(job_id=job_id, status=JobStatus.QUEUED).job_id == job_id


BAD_JOB_IDS = [
    "bt_81J9Z3K4M5N6P7Q8R9S0T1V2W3",  # first ULID char > 7
    "bt_01J9Z3K4M5N6P7Q8R9S0T1V2WU",  # U is not Crockford base32
    "01J9Z3K4M5N6P7Q8R9S0T1V2W3",
    "bt_01j9z3k4m5n6p7q8r9s0t1v2w3",
]


@pytest.mark.parametrize("job_id", BAD_JOB_IDS)
def test_job_id_rejected(job_id: str) -> None:
    with pytest.raises(ValidationError):
        BacktestAccepted(job_id=job_id, status=JobStatus.QUEUED)


def test_problem_forbids_extra() -> None:
    body = {"type": "about:blank", "title": "Bad", "status": 400, "correlation_id": "c-1"}
    assert Problem.model_validate(body).detail is None
    with pytest.raises(ValidationError):
        Problem.model_validate(body | {"instance": "/v1/bars"})


# --- messages ---------------------------------------------------------------------------------

ENVELOPE = {"message_id": "m-1", "correlation_id": "c-1", "produced_at": TS,
            "producer": "tidal-api@1.4.0"}  # fmt: skip


def job_message(**payload_over: Any) -> dict[str, Any]:
    payload = {"job_id": JOB_ID, "parent_job_id": None, "child_index": None, "request": bt()}
    payload |= {"output_prefix": f"backtests/{JOB_ID}/"} | payload_over
    return ENVELOPE | {"schema": "backtest.job/v1", "payload": payload}


PARENT_ID = "bt_01J9Z3K4M5N6P7Q8R9S0T1V2W4"


def test_child_job_message() -> None:
    child = job_message(
        parent_job_id=PARENT_ID, child_index=3, output_prefix=f"backtests/{PARENT_ID}/child-0003/"
    )
    assert BacktestJobMessage.model_validate(child).payload.child_index == 3


def test_backtest_job_message_round_trip() -> None:
    msg = BacktestJobMessage.model_validate(job_message())
    dumped = msg.model_dump(mode="json")
    assert dumped["schema"] == "backtest.job/v1"
    assert dumped["payload"]["request"]["start"] == "946684800000000000"
    assert BacktestJobMessage.model_validate_json(msg.model_dump_json()) == msg
    assert MESSAGE_QUEUES[msg.schema_] == QUEUE_BACKTEST_JOBS


@pytest.mark.parametrize(
    "message",
    [
        job_message() | {"schema": "backtest.job/v2"},
        job_message(request=bt(sweep={"fast": [1, 2]})),
        job_message(output_prefix="md/v1/ticks/"),
        job_message(output_prefix=f"backtests/{JOB_ID}/child-0003/"),  # no parent
        job_message(child_index=3),  # child_index without parent
        job_message(parent_job_id=PARENT_ID, output_prefix=f"backtests/{PARENT_ID}/"),
        job_message(
            parent_job_id=PARENT_ID,
            child_index=3,
            output_prefix=f"backtests/{PARENT_ID}/child-0004/",
        ),
        job_message(
            parent_job_id=PARENT_ID, child_index=3, output_prefix=f"backtests/{JOB_ID}/child-0003/"
        ),
        job_message() | {"produced_at": "2026-10-21T10:00:00"},
        job_message() | {"extra": 1},
    ],
)
def test_bad_job_messages_rejected(message: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BacktestJobMessage.model_validate(message)


METRICS = {"cagr": 0.041, "ann_vol": 0.31, "sharpe": 0.13, "max_drawdown": -0.62,
           "hit_rate": 0.48, "turnover": 3.2, "n_trades": 211}  # fmt: skip


def result_message(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"job_id": JOB_ID, "parent_job_id": None, "status": "succeeded"}
    payload |= {"metrics": METRICS}
    payload |= {"result_sha256": "a" * 64, "error": None, "duration_ms": 8123}
    payload |= {"node_pool": "spot", "attempt": 1} | over
    return ENVELOPE | {"schema": "backtest.result/v1", "payload": payload}


def test_result_message_status_consistency() -> None:
    assert BacktestResultMessage.model_validate(result_message()).payload.status == "succeeded"
    failed = result_message(
        status="failed", metrics=None, result_sha256=None, error={"code": "DATA_GAP", "detail": "x"}
    )
    assert BacktestResultMessage.model_validate(failed).payload.error is not None
    for bad in (result_message(metrics=None), result_message(status="failed"),
                result_message(error={"code": "DATA_GAP", "detail": "x"})):  # fmt: skip
        with pytest.raises(ValidationError):
            BacktestResultMessage.model_validate(bad)


def test_tiering_message() -> None:
    payload = {"table": "md.bar_hot", "hot_window_days": 90, "dry_run": False, "batch_rows": 500000}
    msg = TieringJobMessage.model_validate(
        ENVELOPE | {"schema": "tiering.job/v1", "payload": payload}
    )
    assert msg.payload.table == "md.bar_hot"
    with pytest.raises(ValidationError):
        TieringJobMessage.model_validate(
            ENVELOPE
            | {"schema": "tiering.job/v1", "payload": payload | {"table": "ref.instrument"}}
        )


# --- openapi-aligned shapes -------------------------------------------------------------------


def test_metrics_are_nullable_but_never_nan() -> None:
    undefined = METRICS | {"hit_rate": None, "sharpe": None}
    assert BacktestMetrics.model_validate(undefined).hit_rate is None
    with pytest.raises(ValidationError):
        BacktestMetrics.model_validate(METRICS | {"sharpe": math.nan})
    with pytest.raises(ValidationError):
        BacktestMetrics.model_validate({k: v for k, v in METRICS.items() if k != "cagr"})


def test_status_and_list_shapes() -> None:
    status: dict[str, Any] = {
        "job_id": JOB_ID,
        "status": "running",
        "submitted_at": TS,
        "finished_at": None,
    }
    status |= {"result": None, "children": {"total": 1, "succeeded": 0, "failed": 0}}
    assert BacktestStatus.model_validate(status).result is None
    for missing in ("children", "finished_at", "result"):
        with pytest.raises(ValidationError):
            BacktestStatus.model_validate({k: v for k, v in status.items() if k != missing})
    assert BacktestList.model_validate({"items": [status], "next_cursor": None}).items
    with pytest.raises(ValidationError):
        BarsResponse.model_validate(
            {"instrument_id": "EIA.WTI.SPOT", "interval": "1d", "tier": "cold", "rows": []}
        )  # next_cursor is required (nullable)
    with pytest.raises(ValidationError):
        Bar.model_validate({k: v for k, v in bar().items() if k != "volume"})


def test_latest_spread_admin_and_health_shapes() -> None:
    latest = {"instrument_id": "EIA.WTI.SPOT", "interval": "1d", "bar": bar()}
    assert LatestResponse.model_validate({"tier": "hot", "rows": [latest]}).rows[0].bar.close == 1.5
    with pytest.raises(ValidationError):
        LatestResponse.model_validate({"tier": "cold", "rows": []})
    point = SpreadPoint.model_validate(
        {"ts": TS, "ts_ns": TS_NS, "leg_a": -37.63, "leg_b": 10.01, "spread": -47.64}
    )
    assert point.spread == -47.64
    assert TieringRunAccepted(run_id="r-1", status=JobStatus.QUEUED).run_id == "r-1"
    usage = {"rows": 1, "bytes": 2}
    stats = StorageStats.model_validate({"as_of": TS, "hot": usage, "cold": usage})
    assert stats.cold.bytes == 2
    assert HealthStatus(status="not_ready").status == "not_ready"
    with pytest.raises(ValidationError):
        HealthStatus.model_validate({"status": "degraded"})


def test_optional_non_nullable_fields_dump_without_null() -> None:
    problem = Problem(type="about:blank", title="Bad", status=400, correlation_id="c-1")
    assert "detail" not in problem.model_dump(exclude_none=True)
    req = BacktestRequest.model_validate(bt())
    assert "sweep" not in req.model_dump(mode="json", exclude_none=True)


# --- int64 range and ts/ts_ns consistency -----------------------------------------------------

MAX_ISO, MIN_ISO = "2262-04-11T23:47:16.854775807Z", "1677-09-21T00:12:43.145224192Z"


def test_time_param_int64_edges_accepted() -> None:
    assert to_ts_ns(str(INT64_MAX)) == INT64_MAX
    assert to_ts_ns(MAX_ISO) == INT64_MAX
    assert to_ts_ns(MIN_ISO) == INT64_MIN
    assert to_ts_ns(INT64_MAX) == INT64_MAX
    assert to_ts_ns(INT64_MIN) == INT64_MIN


@pytest.mark.parametrize(
    "bad",
    [
        str(INT64_MAX + 1),
        "9999999999999999999",
        "2262-04-11T23:47:16.854775808Z",
        "1677-09-21T00:12:43.145224191Z",
        "0001-01-01T00:00:00Z",
        "9999-12-31T23:59:59Z",
        INT64_MAX + 1,
        INT64_MIN - 1,
    ],
)
def test_time_param_outside_int64_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="int64"):
        to_ts_ns(bad)
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(bt(start=bad))


def test_ts_must_match_ts_ns() -> None:
    assert Bar.model_validate(bar(ts_ns=TS_NS + 999)).ts_ns == TS_NS + 999  # sub-µs in ts_ns only
    for bad in (TS_NS + 1_000, TS_NS - 1):
        with pytest.raises(ValidationError, match="different instants"):
            Bar.model_validate(bar(ts_ns=bad))
        with pytest.raises(ValidationError, match="different instants"):
            Tick.model_validate(bar_tick(ts_ns=bad))
        with pytest.raises(ValidationError, match="different instants"):
            SpreadPoint.model_validate(
                {"ts": TS, "ts_ns": bad, "leg_a": 1.0, "leg_b": 0.5, "spread": 0.5}
            )
    early = Tick.model_validate(
        bar_tick(ts="1969-12-31T23:59:59.999999Z", ts_ns=-1_000)
    )  # floor before the epoch
    assert early.ts_ns == -1_000
