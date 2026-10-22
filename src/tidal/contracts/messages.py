"""Service Bus message models (messaging-contract.md §1-3)."""

from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from tidal.contracts.models import (
    ULID_PATTERN,
    AnyBacktestRequest,
    BacktestMetrics,
    JobId,
    Sha256,
)
from tidal.contracts.paths import backtest_prefix

QUEUE_BACKTEST_JOBS: Final = "backtest-jobs"
QUEUE_BACKTEST_RESULTS: Final = "backtest-results"
QUEUE_TIERING_JOBS: Final = "tiering-jobs"
TOPIC_INGEST_EVENTS: Final = "ingest-events"
SUBSCRIPTIONS_INGEST_EVENTS: Final = ("stats", "quality-alerts")

#: Value of the ``schema`` field and of the ``schema`` application property.
SCHEMA_BACKTEST_JOB: Final = "backtest.job/v1"
SCHEMA_BACKTEST_RESULT: Final = "backtest.result/v1"
SCHEMA_TIERING_JOB: Final = "tiering.job/v1"
CONTENT_TYPE: Final = "application/json"
#: Max serialised message body (messaging §4); enforced by producers before sending.
MAX_MESSAGE_BYTES: Final = 204_800
#: ``backtests/{job_id}/`` or ``backtests/{parent_job_id}/child-NNNN/`` (messaging §3).
OUTPUT_PREFIX_PATTERN: Final = rf"^backtests/bt_{ULID_PATTERN}/(child-[0-9]{{4}}/)?$"


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_by_name=True, serialize_by_alias=True
    )


class BacktestJob(_Model):
    """``backtest.job/v1`` payload: one (child) backtest with resolved params."""

    job_id: JobId
    parent_job_id: JobId | None = None
    child_index: int | None = Field(default=None, ge=0, le=9999)
    request: AnyBacktestRequest
    output_prefix: str = Field(pattern=OUTPUT_PREFIX_PATTERN)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.request.sweep is not None:
            msg = "backtest.job/v1 carries a resolved request: sweep must be null"
            raise ValueError(msg)
        if (self.parent_job_id is None) != (self.child_index is None):
            msg = "parent_job_id and child_index must be both set or both null"
            raise ValueError(msg)
        expected = backtest_prefix(self.parent_job_id or self.job_id, self.child_index)
        if self.output_prefix != expected:
            msg = f"output_prefix must be {expected!r}"
            raise ValueError(msg)
        return self


class JobError(_Model):
    """Machine-readable failure of a backtest child."""

    code: str = Field(max_length=32, pattern=r"^[A-Z][A-Z0-9_]*$")
    detail: str


class BacktestJobResult(_Model):
    """``backtest.result/v1`` payload."""

    job_id: JobId
    parent_job_id: JobId | None = None
    status: Literal["succeeded", "failed"]
    metrics: BacktestMetrics | None = None
    result_sha256: Sha256 | None = None
    error: JobError | None = None
    duration_ms: int = Field(ge=0)
    node_pool: str
    attempt: int = Field(ge=1)

    @model_validator(mode="after")
    def _status_consistent(self) -> Self:
        ok = self.status == "succeeded"
        has_result = self.metrics is not None and self.result_sha256 is not None
        if ok != has_result or ok == (self.error is not None):
            msg = "succeeded needs metrics+result_sha256 and no error; failed needs error"
            raise ValueError(msg)
        return self


class TieringJob(_Model):
    """``tiering.job/v1`` payload."""

    table: Literal["md.bar_hot", "md.tick_hot"]
    hot_window_days: int = Field(ge=1)
    dry_run: bool
    batch_rows: int = Field(ge=1)


class _Envelope(_Model):
    message_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    produced_at: AwareDatetime
    producer: str = Field(pattern=r"^[a-z0-9-]+@\S+$")


class BacktestJobMessage(_Envelope):
    """Envelope carrying ``backtest.job/v1`` on ``backtest-jobs``."""

    schema_: Annotated[Literal["backtest.job/v1"], Field(alias="schema")]
    payload: BacktestJob


class BacktestResultMessage(_Envelope):
    """Envelope carrying ``backtest.result/v1`` on ``backtest-results``."""

    schema_: Annotated[Literal["backtest.result/v1"], Field(alias="schema")]
    payload: BacktestJobResult


class TieringJobMessage(_Envelope):
    """Envelope carrying ``tiering.job/v1`` on ``tiering-jobs``."""

    schema_: Annotated[Literal["tiering.job/v1"], Field(alias="schema")]
    payload: TieringJob


#: ``schema`` value -> envelope model.
MESSAGE_MODELS: Final[dict[str, type[_Envelope]]] = {
    SCHEMA_BACKTEST_JOB: BacktestJobMessage,
    SCHEMA_BACKTEST_RESULT: BacktestResultMessage,
    SCHEMA_TIERING_JOB: TieringJobMessage,
}
#: ``schema`` value -> queue it travels on.
MESSAGE_QUEUES: Final[dict[str, str]] = {
    SCHEMA_BACKTEST_JOB: QUEUE_BACKTEST_JOBS,
    SCHEMA_BACKTEST_RESULT: QUEUE_BACKTEST_RESULTS,
    SCHEMA_TIERING_JOB: QUEUE_TIERING_JOBS,
}
