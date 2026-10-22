"""Integration-suite hooks (T-128).

``TIDAL_TEST_REQUIRE_SQL=1`` (set by the CI integration job) turns every runtime ``pytest.skip``
raised inside an integration test or fixture (missing ODBC driver, SQL URL/password, Azurite
connection string or reachability) into a failure, so CI cannot pass green with zero coverage.
Without it, local runs keep skipping politely. Marker-based skips are unaffected.
"""

import os
from collections.abc import Generator

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, object, None]:
    """Promote a runtime skip to a failure when ``TIDAL_TEST_REQUIRE_SQL`` is set."""
    outcome = yield
    if os.environ.get("TIDAL_TEST_REQUIRE_SQL") != "1":
        return
    if call.excinfo is None or not call.excinfo.errisinstance(pytest.skip.Exception):
        return
    report = outcome.get_result()  # type: ignore[attr-defined]  # pluggy Result is untyped here
    report.outcome = "failed"
    report.longrepr = f"TIDAL_TEST_REQUIRE_SQL=1: skip not allowed in CI: {call.excinfo.value}"
