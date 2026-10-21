"""Shared pytest configuration. Shared file: orchestrator-only after T-001 (PARALLELISM §1.7)."""

import os

from hypothesis import HealthCheck, settings

# Shared fixtures (T-113): `jwks`, `mint_token` for every test without re-export imports.
pytest_plugins = ["fakes.jwks.plugin"]

# Deterministic property tests (CLAUDE.md §2.2): "ci" never randomises between runs and prints the
# seed of any failure; "dev" explores more examples locally.
settings.register_profile(
    "ci",
    derandomize=True,
    max_examples=200,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("dev", max_examples=100, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
