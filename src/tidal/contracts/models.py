"""Pydantic v2 API models mirroring api-contract.md §3; shapes and names follow openapi.yaml."""

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    RootModel,
    ValidationError,
    WithJsonSchema,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from tidal.contracts.instruments import Instrument, InstrumentId
from tidal.contracts.schemas import QUALITY_MAX, Interval, TickSide

#: ``bt_`` + ULID; a ULID's first character is at most ``7`` (48-bit timestamp).
ULID_PATTERN: Final = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
JOB_ID_PATTERN: Final = rf"^bt_{ULID_PATTERN}$"
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
#: Max children of one sweep (SPEC-04 §3), swept parameters and values per parameter.
MAX_SWEEP_CHILDREN: Final = 200
MAX_SWEEP_KEYS: Final = 3
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
#: openapi ``TimeParam``; validation accepts exactly these strings (when they are real dates).
TIME_PARAM_PATTERN: Final = r"^([0-9]{1,19}|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z)$"
TIME_PARAM_MAX_LENGTH: Final = 35
_ISO_UTC: Final = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+)(?:\.([0-9]{1,9}))?Z")

Finite = Annotated[float, Field(allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Price = Finite  # may be negative (WTI 2020-04-20), never NaN
Quality = Annotated[int, Field(ge=0, le=QUALITY_MAX)]  # reserved bits 5-7 must be 0
Version = Annotated[int, Field(ge=1)]
JobId = Annotated[str, Field(pattern=JOB_ID_PATTERN)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
Count = Annotated[int, Field(ge=0)]


def to_ts_ns(value: object) -> int:
    """Normalise a ``TimeParam`` (ISO-8601 UTC with ``Z``, or epoch-ns digits) to ``ts_ns``.

    Integers pass through (already ``ts_ns``, e.g. when re-validating a dumped model). Fractions are
    kept to the nanosecond.

    Raises:
        ValueError: if a string does not match ``TIME_PARAM_PATTERN`` or is not a valid date-time.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and len(value) <= TIME_PARAM_MAX_LENGTH:
        if re.fullmatch(r"[0-9]{1,19}", value):
            return int(value)
        if (m := _ISO_UTC.fullmatch(value)) and re.fullmatch(TIME_PARAM_PATTERN, value):
            try:
                base = datetime.fromisoformat(m[1]).replace(tzinfo=UTC)
            except ValueError:
                pass
            else:
                delta = base - _EPOCH  # exact integer arithmetic, no float rounding
                return (delta.days * 86_400 + delta.seconds) * 10**9 + int(
                    (m[2] or "").ljust(9, "0")
                )
    msg = "expected an ISO-8601 UTC date-time ending in Z, or epoch nanoseconds as digits"
    raise ValueError(msg)


#: openapi ``TimeParam``: ISO-8601 UTC or epoch-ns string in; held as ``ts_ns``; JSON out as string.
TsNs = Annotated[
    int,
    BeforeValidator(to_ts_ns),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "maxLength": TIME_PARAM_MAX_LENGTH, "pattern": TIME_PARAM_PATTERN}
    ),
]


class Tier(StrEnum):
    """Store tier that served a market-data response (``X-Tidal-Tier``)."""

    HOT = "hot"
    COLD = "cold"
    MIXED = "mixed"


class JobStatus(StrEnum):
    """Backtest job status (openapi ``BacktestJobState``, ``ops.backtest_job.status``)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class Strategy(StrEnum):
    """Backtest strategy (the ``BacktestRequest`` discriminator values)."""

    MA_CROSSOVER = "ma_crossover"
    MEAN_REVERSION = "mean_reversion"
    CALENDAR_SPREAD = "calendar_spread"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthStatus(_Model):
    """Body of ``/v1/health/live`` (``ok``) and ``/v1/health/ready`` (``ready``/``not_ready``)."""

    status: Literal["ok", "ready", "not_ready"]


class Problem(_Model):
    """RFC 7807 problem details. Serialise with ``exclude_none`` (``detail`` is not nullable)."""

    type: str = Field(json_schema_extra={"format": "uri"})
    title: str
    status: int
    detail: str | SkipJsonSchema[None] = None
    correlation_id: str


class InstrumentList(_Model):
    """``GET /v1/instruments``."""

    items: list[Instrument]


class Bar(_Model):
    """One OHLCV bar; ``volume`` is ``null`` when the source has none (data-contracts §3.1)."""

    ts: AwareDatetime
    ts_ns: int
    open: Price
    high: Price
    low: Price
    close: Price
    volume: NonNegative | None
    quality: Quality
    version: Version

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Self:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            msg = "require low <= min(open, close) and high >= max(open, close)"
            raise ValueError(msg)
        return self


class BarsResponse(_Model):
    """``GET /v1/bars``."""

    instrument_id: InstrumentId
    interval: Interval
    tier: Tier
    rows: list[Bar]
    next_cursor: str | None


class Tick(_Model):
    """One tick (data-contracts §2; internal columns omitted)."""

    ts: AwareDatetime
    ts_ns: int
    price: Price
    size: NonNegative
    side: TickSide
    quality: Quality
    version: Version


class TicksResponse(_Model):
    """``GET /v1/ticks``."""

    instrument_id: InstrumentId
    tier: Tier
    rows: list[Tick]
    next_cursor: str | None


class LatestBar(_Model):
    """Last bar of one instrument."""

    instrument_id: InstrumentId
    interval: Interval
    bar: Bar


class LatestResponse(_Model):
    """``GET /v1/latest`` (hot store only)."""

    tier: Literal[Tier.HOT]
    rows: list[LatestBar]


class SpreadPoint(_Model):
    """One timestamp where both legs have a bar; ``spread = leg_a - leg_b`` (closes)."""

    ts: AwareDatetime
    ts_ns: int
    leg_a: Price
    leg_b: Price
    spread: Price


class SpreadResponse(_Model):
    """``GET /v1/spreads``."""

    leg_a: InstrumentId
    leg_b: InstrumentId
    interval: Interval
    tier: Tier
    rows: list[SpreadPoint]


class BacktestCosts(_Model):
    """Transaction costs applied to traded notional."""

    fee_bps: NonNegative
    slippage_bps: NonNegative


class MaCrossoverParams(_Model):
    """``ma_crossover`` parameters (SPEC-05 §2)."""

    fast: int = Field(ge=1)
    slow: int = Field(ge=1)


class MeanReversionParams(_Model):
    """``mean_reversion`` / ``calendar_spread`` parameters (SPEC-05 §2)."""

    lookback: int = Field(ge=2)
    z_entry: float = Field(gt=0, allow_inf_nan=False)
    z_exit: float = Field(ge=0, allow_inf_nan=False)


SweepValues = Annotated[list[Finite], Field(min_length=1, max_length=MAX_SWEEP_CHILDREN)]


class BacktestRequestBase(_Model):
    """Fields shared by every strategy request; subclasses add ``strategy``, ids and ``params``."""

    interval: Literal[Interval.M1, Interval.H1, Interval.D1]
    start: TsNs
    end: TsNs
    costs: BacktestCosts
    seed: int
    sweep: (
        Annotated[dict[str, SweepValues], Field(min_length=1, max_length=MAX_SWEEP_KEYS)]
        | SkipJsonSchema[None]
    ) = None  # optional, not nullable in openapi.yaml: serialise with exclude_none

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.start >= self.end:
            msg = "start must be before end"
            raise ValueError(msg)
        params = dict(self).get("params")
        if self.sweep is None or not isinstance(params, BaseModel):
            return self
        params_model = type(params)
        if not set(self.sweep) <= set(params_model.model_fields):
            msg = f"sweep keys must be strategy params: {sorted(params_model.model_fields)}"
            raise ValueError(msg)
        if self.sweep_size > MAX_SWEEP_CHILDREN:
            msg = f"sweep expands to {self.sweep_size} jobs (max {MAX_SWEEP_CHILDREN})"
            raise ValueError(msg)
        base = params.model_dump()
        for key, values in self.sweep.items():
            for value in values:
                try:
                    params_model.model_validate(base | {key: value})
                except ValidationError as exc:
                    msg = f"sweep value {key}={value} is not a valid parameter"
                    raise ValueError(msg) from exc
        return self

    @property
    def sweep_size(self) -> int:
        """Number of child jobs (1 without a sweep)."""
        return math.prod(len(v) for v in (self.sweep or {}).values())


class MaCrossoverRequest(BacktestRequestBase):
    """``ma_crossover`` on one instrument."""

    strategy: Literal["ma_crossover"]
    instrument_ids: list[InstrumentId] = Field(min_length=1, max_length=1)
    params: MaCrossoverParams


class MeanReversionRequest(BacktestRequestBase):
    """``mean_reversion`` on one instrument."""

    strategy: Literal["mean_reversion"]
    instrument_ids: list[InstrumentId] = Field(min_length=1, max_length=1)
    params: MeanReversionParams


class CalendarSpreadRequest(BacktestRequestBase):
    """``calendar_spread`` on ``instrument_ids[0] - instrument_ids[1]`` (e.g. WTI FUT1 - FUT2)."""

    strategy: Literal["calendar_spread"]
    instrument_ids: list[InstrumentId] = Field(min_length=2, max_length=2)
    params: MeanReversionParams


#: One strategy request, discriminated on ``strategy``; also the ``backtest.job/v1`` request.
AnyBacktestRequest = Annotated[
    MaCrossoverRequest | MeanReversionRequest | CalendarSpreadRequest,
    Field(discriminator="strategy"),
]


class BacktestRequest(RootModel[AnyBacktestRequest]):
    """``POST /v1/backtests``; ``sweep`` fans out into the cartesian product of its values."""


class BacktestAccepted(_Model):
    """``202`` body of ``POST /v1/backtests``."""

    job_id: JobId
    status: Literal[JobStatus.QUEUED]


class BacktestChildren(_Model):
    """Child job counters."""

    total: Count
    succeeded: Count
    failed: Count


Metric = Finite | None  # null when undefined (e.g. hit_rate with 0 closed trades), never NaN


class BacktestMetrics(_Model):
    """Metrics per SPEC-05 §3; a value without mathematical definition is ``null``."""

    cagr: Metric
    ann_vol: Metric
    sharpe: Metric
    max_drawdown: Metric
    hit_rate: Metric
    turnover: Metric
    n_trades: Count


class BacktestResult(_Model):
    """Result of a finished backtest."""

    metrics: BacktestMetrics
    equity_url: str
    trades_url: str
    result_sha256: Sha256


class BacktestStatus(_Model):
    """``GET /v1/backtests/{job_id}``."""

    job_id: JobId
    status: JobStatus
    children: BacktestChildren
    submitted_at: AwareDatetime
    finished_at: AwareDatetime | None
    result: BacktestResult | None


class BacktestList(_Model):
    """``GET /v1/backtests`` (own jobs)."""

    items: list[BacktestStatus]
    next_cursor: str | None


class TieringRunAccepted(_Model):
    """``202`` body of ``POST /v1/admin/tiering/run``."""

    run_id: str
    status: Literal[JobStatus.QUEUED]


class TierUsage(_Model):
    """Rows and bytes held in one tier."""

    rows: Count
    bytes: Count


class StorageStats(_Model):
    """``GET /v1/admin/storage/stats``: rows/bytes per tier from the last stats run."""

    as_of: AwareDatetime
    hot: TierUsage
    cold: TierUsage


#: Every exported API model; names equal openapi.yaml components (``HealthStatus``,
#: ``TierUsage`` and the params models are inline schemas there).
API_MODELS: Final[tuple[type[BaseModel], ...]] = (
    HealthStatus,
    Problem,
    Instrument,
    InstrumentList,
    Bar,
    BarsResponse,
    Tick,
    TicksResponse,
    LatestBar,
    LatestResponse,
    SpreadPoint,
    SpreadResponse,
    BacktestCosts,
    BacktestRequestBase,
    MaCrossoverRequest,
    MeanReversionRequest,
    CalendarSpreadRequest,
    BacktestRequest,
    BacktestAccepted,
    BacktestChildren,
    BacktestMetrics,
    BacktestResult,
    BacktestStatus,
    BacktestList,
    TieringRunAccepted,
    StorageStats,
)
