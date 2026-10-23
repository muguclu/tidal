"""The rendered worker env must build ``tidal.common.config.Settings`` (T-307 review, blocking 2).

Renders the chart with ``values-dev.yaml`` through the pinned ``alpine/helm`` image and feeds the
ScaledJob container env to ``Settings.from_env``. Skipped when Docker or the image is unavailable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tidal.common.config import Settings

CHART = Path(__file__).resolve().parents[3] / "deploy" / "helm" / "tidal"
HELM_IMAGE = "alpine/helm:4.3.1"


def _render(*extra: str) -> subprocess.CompletedProcess[str]:
    cmd = [
        "docker", "run", "--rm", "-v", f"{CHART}:/c", HELM_IMAGE,
        "template", "tidal", "/c", "-f", "/c/values-dev.yaml", *extra,
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)  # noqa: S603


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, object]]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    proc = _render()
    if proc.returncode != 0 and "Unable to find image" in proc.stderr + proc.stdout:
        pytest.skip("helm image not available")
    if proc.returncode != 0 and "docker" in proc.stderr.lower() and "daemon" in proc.stderr.lower():
        pytest.skip("docker daemon not available")
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def test_worker_env_builds_settings(rendered: list[dict[str, object]]) -> None:
    job = next(d for d in rendered if d["kind"] == "ScaledJob")
    container = job["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0]  # type: ignore[index]
    env = {e["name"]: e["value"] for e in container["env"]}
    settings = Settings.from_env(env)
    assert settings.env == "dev"
    assert settings.sql.auth == "entra_mi"
    assert settings.storage.use_managed_identity is True
    assert settings.query.audience == "api://tidal-query"


def test_secret_like_key_is_rejected(rendered: list[dict[str, object]]) -> None:
    assert rendered
    proc = _render("--set", "worker.config.TIDAL_STORAGE__CONNECTION_STRING=x")
    assert proc.returncode != 0
    assert "secret" in proc.stderr
