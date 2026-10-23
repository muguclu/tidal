"""Post-deploy smoke tests (T-311). Run by .github/workflows/deploy.yml, never by the normal suites.

pyproject registers no ``smoke`` marker (and uses ``--strict-markers``), so it is registered
here. Every test skips cleanly unless its target URL is set in the environment.
"""

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``smoke`` marker (kept out of pyproject, which this task does not own)."""
    config.addinivalue_line(
        "markers", "smoke: post-deploy check against a live environment (needs TIDAL_SMOKE_*_URL)"
    )


def _base(var: str) -> str:
    url = os.environ.get(var, "").strip().rstrip("/")
    if not url:
        pytest.skip(f"{var} is not set")
    if not url.startswith("https://"):
        pytest.fail(f"{var} must be an https URL")
    return url


@pytest.fixture(scope="session")
def api_url() -> str:
    """Base URL of tidal-api (App Service)."""
    return _base("TIDAL_SMOKE_API_URL")


@pytest.fixture(scope="session")
def query_url() -> str:
    """Base URL of tidal-query (Container App)."""
    return _base("TIDAL_SMOKE_QUERY_URL")
