"""Backtest endpoints vs docs/contracts/openapi.yaml (T-304): in-process guard plus fuzzing."""

from pathlib import Path
from typing import Any

import schemathesis
import yaml
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import (
    allow_header_conformance,
    negative_data_rejection,
    positive_data_acceptance,
)

from fakes.backtest_store import FakeBacktestStore
from fakes.bus import FakeBus
from tidal.api.app import create_app
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock

PATHS = {"/v1/backtests", "/v1/backtests/{job_id}"}
CONTRACT = Path(__file__).resolve().parents[2] / "docs/contracts/openapi.yaml"
DOC: dict[str, Any] = yaml.safe_load(CONTRACT.read_text("utf-8"))


def _app() -> Any:  # Any: FastAPI
    app = create_app(
        Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"}),
        bus=FakeBus(),
        clock=FixedClock(1_700_000_000 * 10**9),
    )
    app.state.backtest_store = FakeBacktestStore()
    app.state.rate_limiter.check = lambda _oid: None  # fuzzing exceeds 60 requests/min
    return app


def test_generated_operations_match_contract() -> None:
    generated = _app().openapi()
    for path in PATHS:
        for method, want in DOC["paths"][path].items():
            got = generated["paths"][path][method]
            assert got["operationId"] == want["operationId"]
            assert got["x-tidal-min-role"] == want["x-tidal-min-role"]
            assert set(got["responses"]) - {"422"} == set(
                want["responses"]
            )  # 422: FastAPI default, same on every router
            assert "security" not in want  # inherited global scheme
            assert {p["name"] for p in got.get("parameters", [])} == {
                p["name"] if "name" in p else p["$ref"].rsplit("/", 1)[1].lower()
                for p in want.get("parameters", [])
            }
    assert generated["security"] == DOC["security"]
    body = generated["paths"]["/v1/backtests"]["post"]["requestBody"]
    assert body["required"] is True
    assert body["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BacktestRequest"
    }


def _load() -> Any:  # Any: schemathesis schema
    doc = dict(DOC)
    doc["paths"] = {p: v for p, v in DOC["paths"].items() if p in PATHS}
    schema = schemathesis.openapi.from_dict(doc)
    schema.app = _app()
    schema.config.update(base_url="http://testserver")
    return schema


schema = _load()


@schema.parametrize()  # type: ignore[untyped-decorator]  # schemathesis ships no typed decorator
@settings(max_examples=25, deadline=None, suppress_health_check=list(HealthCheck))
def test_backtest_api_conforms_to_contract(case: Any) -> None:
    """Status codes, headers, content types and bodies match the contract."""
    # allow_header_conformance: GET and POST are separate Starlette routes on one path, so a 405
    # advertises only the first route's method (framework behaviour, not a contract difference).
    # negative_data_rejection: a sweep axis over 200 values violates maxItems but is a documented
    # 413 (product limit), not a 400.
    # Schema-valid submissions can be semantically invalid (start >= end, sweep keys, 413).
    case.call_and_validate(
        excluded_checks=[
            positive_data_acceptance,
            allow_header_conformance,
            negative_data_rejection,
        ]
    )
