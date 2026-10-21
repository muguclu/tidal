"""TidalError hierarchy -> contract Problem (api-contract §1, §4)."""

from pathlib import Path

import pytest

from tidal.common import errors
from tidal.common.errors import (
    ConfigError,
    DependencyUnavailable,
    Forbidden,
    NotFound,
    RangeTooLarge,
    RateLimited,
    TidalError,
    Unauthenticated,
    ValidationFailed,
)
from tidal.contracts.models import Problem

CID = "01JABCDEFGHJKMNPQRSTVWXYZ0"
CASES: list[tuple[TidalError, int, str]] = [
    (ValidationFailed("end must be after start"), 400, "validation-failed"),
    (Unauthenticated(), 401, "unauthenticated"),
    (Forbidden(), 403, "forbidden"),
    (NotFound("unknown instrument"), 404, "not-found"),
    (RangeTooLarge("ticks are limited to 31 days per call"), 413, "range-too-large"),
    (RateLimited(retry_after=12), 429, "rate-limited"),
    (DependencyUnavailable(), 503, "dependency-unavailable"),
    (ConfigError(), 500, "config-error"),
    (TidalError(), 500, "internal"),
]


@pytest.mark.parametrize(("error", "status", "slug"), CASES, ids=[c[2] for c in CASES])
def test_error_maps_to_problem(error: TidalError, status: int, slug: str) -> None:
    problem = error.to_problem(CID)
    assert isinstance(problem, Problem)
    assert problem.status == status == error.status
    assert problem.type == f"https://tidal.example/problems/{slug}"
    assert problem.title == error.title
    assert problem.correlation_id == CID
    assert problem.detail == error.detail
    dumped = problem.model_dump(exclude_none=True)
    assert set(dumped) <= {"type", "title", "status", "detail", "correlation_id"}


def test_all_errors_are_tidal_errors() -> None:
    assert all(isinstance(e, TidalError) for e, *_ in CASES)


def test_detail_omitted_when_not_given() -> None:
    assert "detail" not in Forbidden().to_problem(CID).model_dump(exclude_none=True)


def test_cause_text_never_reaches_detail() -> None:
    def fail() -> None:
        try:
            raise RuntimeError("SELECT * FROM t; password=hunter2")
        except RuntimeError as exc:
            raise DependencyUnavailable("SQL is unreachable") from exc

    with pytest.raises(DependencyUnavailable) as exc_info:
        fail()
    assert exc_info.value.__cause__ is not None
    problem = exc_info.value.to_problem(CID).model_dump_json()
    for leaked in ("hunter2", "SELECT", "Traceback"):
        assert leaked not in problem


def test_rate_limited_retry_after() -> None:
    assert RateLimited(retry_after=30).retry_after == 30
    with pytest.raises(ValueError, match="retry_after"):
        RateLimited(retry_after=-1)


def test_str_is_detail_or_title() -> None:
    assert str(NotFound("job not found")) == "job not found"
    assert str(NotFound()) == "Not found"


def test_module_has_no_web_framework_imports() -> None:
    text = Path(errors.__file__).read_text(encoding="utf-8")
    assert "fastapi" not in text
    assert "starlette" not in text
