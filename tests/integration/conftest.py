"""Integration-suite hooks (T-128).

``TIDAL_TEST_REQUIRE_SQL=1`` (set by the CI integration job) turns every runtime ``pytest.skip``
raised inside an integration test or fixture (missing ODBC driver, SQL URL/password, Azurite
connection string or reachability) into a failure, so CI cannot pass green with zero coverage.
Without it, local runs keep skipping politely. Marker-based skips are unaffected.
"""

import os
from collections.abc import Generator

import pytest


def _marker_skip(item: pytest.Item) -> bool:
    """True when the item carries a skip marker, or a skipif whose condition is not plain False."""
    if any(True for _ in item.iter_markers("skip")):
        return True
    return any(m.args[:1] != (False,) for m in item.iter_markers("skipif"))


@pytest.fixture
def hidden_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide ``TIDAL_TEST_*`` from the process env: ``Settings`` rejects unknown ``TIDAL_*`` keys.

    Request it in any test that calls ``Settings.from_env`` directly.
    """
    for key in [k for k in os.environ if k.startswith("TIDAL_TEST_")]:
        monkeypatch.delenv(key)


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
    if _marker_skip(item):
        return  # declared with @pytest.mark.skip / skipif: not a missing prerequisite
    report = outcome.get_result()  # type: ignore[attr-defined]  # pluggy Result is untyped here
    report.outcome = "failed"
    report.longrepr = f"TIDAL_TEST_REQUIRE_SQL=1: skip not allowed in CI: {call.excinfo.value}"
