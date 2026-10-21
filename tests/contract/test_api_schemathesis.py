"""Contract fuzzing (T-111): schemathesis 4.x against the ASGI app with fakes.

The schema is ``docs/contracts/openapi.yaml`` restricted to the implemented read paths. Runs in
the normal suite (about 15 s with 25 examples per operation), so it carries no extra marker.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import schemathesis
import yaml
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import positive_data_acceptance

from fakes.reader import FakeReader
from tidal.api.app import create_app
from tidal.common.config import Settings
from tidal.common.timeutil import from_ts_ns

READ_PATHS = {
    "/v1/instruments",
    "/v1/instruments/{id}",
    "/v1/bars",
    "/v1/ticks",
    "/v1/latest",
    "/v1/spreads",
}
CONTRACT = Path(__file__).resolve().parents[2] / "docs/contracts/openapi.yaml"


class TickingClock:
    """Advances one second per read, so the 60/min rate limit never trips while fuzzing."""

    def __init__(self) -> None:
        self._now = from_ts_ns(1_587_340_800 * 10**9)

    def now(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def _load() -> Any:
    doc: dict[str, Any] = yaml.safe_load(CONTRACT.read_text("utf-8"))
    doc["paths"] = {p: v for p, v in doc["paths"].items() if p in READ_PATHS}
    app = create_app(
        Settings.from_env({"TIDAL_AUTH__DISABLED": "true"}),
        reader=FakeReader(),
        clock=TickingClock(),
    )
    schema = schemathesis.openapi.from_dict(doc)
    schema.app = app
    schema.config.update(base_url="http://testserver")
    return schema


schema = _load()


@schema.parametrize()  # type: ignore[untyped-decorator]  # schemathesis ships no typed decorator
@settings(max_examples=25, deadline=None, suppress_health_check=list(HealthCheck))
def test_read_api_conforms_to_contract(case: Any) -> None:
    """Responses match the contract: status codes, headers, content types and schemas."""
    # positive_data_acceptance is excluded: some schema-valid inputs are semantically invalid and
    # documented as 400/413 (HMAC-signed cursor, end <= start, range limits of SPEC-04 section 3).
    case.call_and_validate(excluded_checks=[positive_data_acceptance])


def test_operations_cover_all_read_paths() -> None:
    covered = {r.ok().path for r in schema.get_all_operations() if hasattr(r, "ok")}
    assert covered == READ_PATHS
