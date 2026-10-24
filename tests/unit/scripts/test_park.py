"""park.sh / unpark.sh against a stateful fake ``az`` on PATH (T-313).

The fake keeps resource state in files, logs every call, and mutates state on change commands, so a
second run must issue no state-changing command.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
BASH = shutil.which("bash")

FAKE_AZ = r"""#!/usr/bin/env bash
set -eu
d="$FAKE_AZ_STATE"
echo "$*" >>"$d/calls.log"
name=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-n" ]; then name="$a"; fi
  prev="$a"
done
get() { cat "$d/$1" 2>/dev/null || echo "$2"; }
case "$1 $2 $3" in
  "logic workflow show") get "logic.$name" Enabled ;;
  "logic workflow update")
    echo "$*" | sed -n 's/.*--state \([A-Za-z]*\).*/\1/p' >"$d/logic.$name"
    echo CHANGE >>"$d/changes.log" ;;
  "vm show -d") get "vm.$name" "VM running" ;;
  "vm deallocate "*) echo "VM deallocated" >"$d/vm.$name"; echo CHANGE >>"$d/changes.log" ;;
  "vm start "*) echo "VM running" >"$d/vm.$name"; echo CHANGE >>"$d/changes.log" ;;
  "containerapp job show") get "job.$name" Running ;;
  "containerapp job stop") echo Suspended >"$d/job.$name"; echo CHANGE >>"$d/changes.log" ;;
  "containerapp job start") echo Running >"$d/job.$name"; echo CHANGE >>"$d/changes.log" ;;
  "webapp show "*) get "web.$name" Running ;;
  "webapp stop "*) echo Stopped >"$d/web.$name"; echo CHANGE >>"$d/changes.log" ;;
  "webapp start "*) echo Running >"$d/web.$name"; echo CHANGE >>"$d/changes.log" ;;
  "aks show "*) get "aks.$name" Running ;;
  "aks stop "*) echo Stopped >"$d/aks.$name"; echo CHANGE >>"$d/changes.log" ;;
  "aks start "*) echo Running >"$d/aks.$name"; echo CHANGE >>"$d/changes.log" ;;
  *) echo "unexpected az call: $*" >&2; exit 99 ;;
esac
"""

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    az = bindir / "az"
    az.write_text(FAKE_AZ, newline="\n")
    az.chmod(az.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    e = {k: v for k, v in os.environ.items() if not k.startswith(("TIDAL_", "AZURE_"))}
    e.update(
        PATH=f"{bindir}{os.pathsep}{e['PATH']}",
        FAKE_AZ_STATE=str(state),
        TIDAL_RESOURCE_GROUP="rg-x",
        TIDAL_AKS_NAME="aks-x",
        TIDAL_WEBAPP_NAME="app-x",
        TIDAL_JOB_NAMES="caj-a caj-b",
        TIDAL_VM_NAME="vm-x",
        TIDAL_LOGIC_APP_NAME="la-x",
    )
    return e


def _run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(  # noqa: S603
        [BASH, str(SCRIPTS / script), *args],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def _changes(env: dict[str, str]) -> int:
    p = Path(env["FAKE_AZ_STATE"]) / "changes.log"
    return len(p.read_text().splitlines()) if p.exists() else 0


def _reset_changes(env: dict[str, str]) -> None:
    (Path(env["FAKE_AZ_STATE"]) / "changes.log").unlink(missing_ok=True)


def test_park_then_park_again_is_noop(env: dict[str, str]) -> None:
    first = _run("park.sh", env)
    assert first.returncode == 0, first.stderr
    assert _changes(env) == 6  # logic, vm, 2 jobs, webapp, aks
    assert "terraform apply -var park=true" in first.stdout
    _reset_changes(env)
    second = _run("park.sh", env)
    assert second.returncode == 0, second.stderr
    assert _changes(env) == 0
    assert "skipped" in second.stdout


def test_unpark_reverses_and_is_idempotent(env: dict[str, str]) -> None:
    assert _run("park.sh", env).returncode == 0
    _reset_changes(env)
    first = _run("unpark.sh", env)
    assert first.returncode == 0, first.stderr
    assert _changes(env) == 6
    calls = (Path(env["FAKE_AZ_STATE"]) / "calls.log").read_text().splitlines()
    starts = [c for c in calls if " start " in f" {c} " or "--state Enabled" in c]
    assert starts[0].startswith("aks start")  # slowest first
    assert starts[-1].startswith("logic workflow update")  # Logic App last
    _reset_changes(env)
    assert _run("unpark.sh", env).returncode == 0
    assert _changes(env) == 0


def test_dry_run_makes_no_az_calls(env: dict[str, str]) -> None:
    for script in ("park.sh", "unpark.sh"):
        res = _run(script, env, "--dry-run")
        assert res.returncode == 0, res.stderr
        assert "[dry-run] az " in res.stdout
    assert not (Path(env["FAKE_AZ_STATE"]) / "calls.log").exists()


def test_missing_resource_group_fails(env: dict[str, str]) -> None:
    del env["TIDAL_RESOURCE_GROUP"]
    assert _run("park.sh", env).returncode == 2


def test_rejects_malformed_name(env: dict[str, str]) -> None:
    env["TIDAL_VM_NAME"] = "vm; rm -rf /"
    assert _run("park.sh", env, "--dry-run").returncode == 2
