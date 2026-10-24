"""The rendered worker env must build ``tidal.common.config.Settings`` (T-307 review, blocking 2).

Renders the chart with ``values-dev.yaml`` through the pinned ``alpine/helm`` image and feeds the
ScaledJob container env to ``Settings.from_env``. Skipped when Docker or the image is unavailable,
except under ``CI=true`` where a missing Docker or helm is a failure (T-409, ADR-0008 R-19).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any  # parsed YAML is untyped; narrowed by assertions

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


def _unavailable(reason: str) -> None:
    """Skip locally, but fail when running in CI so the check can never silently vanish."""
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(f"{reason} (CI=true: chart checks must run, not skip)")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, object]]:
    if shutil.which("docker") is None:
        _unavailable("docker not available")
    proc = _render()
    if proc.returncode != 0 and "Unable to find image" in proc.stderr + proc.stdout:
        _unavailable("helm image not available")
    if proc.returncode != 0 and "docker" in proc.stderr.lower() and "daemon" in proc.stderr.lower():
        _unavailable("docker daemon not available")
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


def test_non_local_values_pin_image_by_digest(rendered: list[dict[str, object]]) -> None:
    """T-405: no floating tags; every non-local values file sets a well-formed image.digest."""
    for path in sorted(CHART.glob("values-*.yaml")):
        image = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("image", {})
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(image.get("digest", ""))), path.name
        assert image.get("tag") != "latest", path.name
    images = re.findall(r"image:\s*\"?([^\s\"]+)", yaml.safe_dump_all(rendered))
    assert images, "no container images rendered"
    assert all("@sha256:" in i for i in images), images


_HOST_KEYS = ("privileged", "hostPath", "hostNetwork", "hostPID", "hostIPC")


def _pod_specs(rendered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every pod spec in the render: the ScaledJob job template plus any workload kinds."""
    specs: list[dict[str, Any]] = []
    for d in rendered:
        spec = d.get("spec") or {}
        if d["kind"] == "ScaledJob":
            specs.append(spec["jobTargetRef"]["template"]["spec"])
        elif d["kind"] == "Pod":
            specs.append(spec)
        elif d["kind"] in {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"}:
            specs.append(spec["template"]["spec"])
        elif d["kind"] == "CronJob":
            specs.append(spec["jobTemplate"]["spec"]["template"]["spec"])
    return specs


def _walk_keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _walk_keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _walk_keys(v)}
    return set()


def test_pod_spec_has_no_host_access(rendered: list[dict[str, Any]]) -> None:
    """T-409: no privileged, hostPath, hostNetwork, hostPID or hostIPC anywhere in a pod spec."""
    specs = _pod_specs(rendered)
    assert specs, "no pod spec rendered"
    for spec in specs:
        assert not _walk_keys(spec) & set(_HOST_KEYS), _walk_keys(spec) & set(_HOST_KEYS)
        for c in [*spec.get("containers", []), *spec.get("initContainers", [])]:
            sc = c.get("securityContext", {})
            assert sc.get("privileged") is not True
            assert sc.get("allowPrivilegeEscalation") is False


def test_pod_spec_is_hardened(rendered: list[dict[str, Any]]) -> None:
    """T-409: non-root, read-only root filesystem, all capabilities dropped, on every container."""
    for spec in _pod_specs(rendered):
        pod_sc = spec.get("securityContext", {})
        containers = [*spec.get("containers", []), *spec.get("initContainers", [])]
        assert containers
        for c in containers:
            sc = c.get("securityContext", {})
            assert (sc.get("runAsNonRoot") or pod_sc.get("runAsNonRoot")) is True, c["name"]
            assert sc.get("readOnlyRootFilesystem") is True, c["name"]
            assert sc.get("capabilities", {}).get("drop") == ["ALL"], c["name"]
            assert not sc.get("capabilities", {}).get("add"), c["name"]
