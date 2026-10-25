# ruff: noqa: PT018, PLC0415
"""CLI-level acceptance tests (SPEC-01 §5) on SQLite + LocalStore with MockTransport upstreams."""

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from tidal.common import config
from tidal.common.config import Settings
from tidal.common.errors import ConfigError
from tidal.common.logging import configure_logging
from tidal.common.timeutil import FixedClock, to_ts_ns
from tidal.contracts.schemas import Interval
from tidal.ingest import cli
from tidal.ingest.cli import Context
from tidal.ingest.router import Routed, read_part
from tidal.ingest.sources import synthetic as syn
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import AzureBlobStore, LocalStore

FIX = Path(__file__).parents[2] / "fixtures"
DAY = 86_400 * 10**9
WTI, FUT1 = "EIA.WTI.SPOT", "EIA.WTI.FUT1"
RUNNER = CliRunner()


def ns(day: str) -> int:
    parsed = datetime.fromisoformat(day)
    return to_ts_ns(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))


@pytest.fixture
def engine() -> Engine:
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(eng, "connect")
    def _attach(dbapi: Any, _rec: object) -> None:
        for schema in ("ref", "md", "ops"):
            dbapi.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    sr.metadata.create_all(eng)
    return eng


def make_ctx(
    engine: Engine,
    root: Path,
    now: str,
    *,
    hot_days: int = 90,
    env: dict[str, str] | None = None,
) -> Context:
    settings = Settings.from_env(
        {"TIDAL_ENV": "local", "TIDAL_HOT_WINDOW_DAYS": str(hot_days), **(env or {})}
    )
    clock = FixedClock(ns(now))
    store = LocalStore(root)
    return Context(settings, store, sr.SqlRepository(engine, clock), clock)


def adv(ctx: Context) -> None:
    assert isinstance(ctx.clock, FixedClock)
    ctx.clock.advance(DAY)


# -- storage / context ---------------------------------------------------------------------------
def test_build_store_local_default_and_azure_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    store = cli.build_store(Settings.from_env({"TIDAL_ENV": "local"}))
    assert isinstance(store, LocalStore) and store.root == cli.LOCAL_ROOT.resolve()

    created: list[str] = []

    class Container:
        def __init__(self, name: str) -> None:
            self.name = name

        def exists(self) -> bool:
            return self.name == "raw"

    class Service:
        def get_container_client(self, name: str) -> Container:
            return Container(name)

        def create_container(self, name: str) -> None:
            created.append(name)

    from azure.storage import blob

    monkeypatch.setattr(
        blob.BlobServiceClient, "from_connection_string", staticmethod(lambda _c: Service())
    )
    conn = {"TIDAL_STORAGE__CONNECTION_STRING": "UseDevelopmentStorage=true"}
    assert isinstance(
        cli.build_store(Settings.from_env({"TIDAL_ENV": "local", **conn})), AzureBlobStore
    )
    assert created == ["marketdata", "reference", "backtests"]

    import azure.identity as ident

    monkeypatch.setattr(ident, "DefaultAzureCredential", object)
    monkeypatch.setattr(blob, "BlobServiceClient", lambda url, cred: Service())
    managed = Settings.from_env(
        {
            "TIDAL_ENV": "dev",
            "TIDAL_AUTH__TENANT_ID": "00000000-0000-0000-0000-000000000001",
            "TIDAL_SQL__AUTH": "entra_mi",
            "TIDAL_SQL__SERVER": "x.database.windows.net",
            "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
            "TIDAL_STORAGE__ACCOUNT_URL": "https://a.b",
        }
    )
    assert isinstance(cli.build_store(managed), AzureBlobStore)


def test_build_context_wires_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TIDAL_ENV", "local")
    monkeypatch.setenv("TIDAL_CLOCK__NOW", "2025-01-02T03:04:05+00:00")
    config.get_settings.cache_clear()
    try:
        ctx = cli.build_context()
    finally:
        config.get_settings.cache_clear()
    assert ctx.clock.now_ns() == ns("2025-01-02T03:04:05")
    assert isinstance(ctx.store, LocalStore)


# -- synthetic -----------------------------------------------------------------------------------
SYNTH_ENV = {"TIDAL_SYNTH__START": "2025-01-06", "TIDAL_SYNTH__END": "2025-01-10"}


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*.parquet"))
    }


def synth_run(engine: Engine, root: Path, now: str, *, seed: int = 1, units: int = 2) -> Context:
    ctx = make_ctx(engine, root, now, hot_days=2, env=SYNTH_ENV)
    s = ctx.settings.synth
    todo = syn.plan("S", s.start, s.end)[:units]
    cli.run_synth(ctx, "S", seed, 1, todo)
    return ctx


def test_synth_same_seed_twice_gives_identical_file_hashes(engine: Engine, tmp_path: Path) -> None:
    synth_run(engine, tmp_path / "a", "2025-01-10T23:59:59+00:00")
    other = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(other, "connect", lambda dbapi, _r: [
        dbapi.execute(f"ATTACH DATABASE ':memory:' AS {s}") for s in ("ref", "md", "ops")
    ])  # fmt: skip
    sr.metadata.create_all(other)
    synth_run(other, tmp_path / "b", "2025-01-10T23:59:59+00:00")
    a, b = tree_hashes(tmp_path / "a"), tree_hashes(tmp_path / "b")
    assert a and a == b
    datasets = {p.split("/")[2] for p in a}
    assert datasets == {"ticks", "bars_1s", "bars_1m", "bars_1h", "bars_1d"}
    assert any("bars_1d" in p and "month=00" in p for p in a)
    assert any("ticks" in p and "month=01" in p for p in a)
    other_ctx = synth_run(engine, tmp_path / "c", "2025-01-10T23:59:59+00:00", seed=2)
    assert isinstance(other_ctx.store, LocalStore)
    c = tree_hashes(other_ctx.store.root)
    assert c != a


def test_synth_routes_hot_window_to_sql_and_tracks_instrument_bounds(
    engine: Engine, tmp_path: Path
) -> None:
    ctx = synth_run(engine, tmp_path, "2025-01-10T23:59:59+00:00", units=1)
    iid = syn.plan("S", date(2025, 1, 6), date(2025, 1, 10))[0].instrument_id
    cutoff = ns("2025-01-10T23:59:59") - 2 * DAY
    hot = ctx.repo.read_ticks(iid, 0, ns("2026-01-01"))
    assert hot.num_rows > 0 and hot.column("ts_ns").to_numpy().min() >= cutoff
    assert ctx.repo.read_bars(iid, Interval.H1, 0, ns("2026-01-01")).num_rows > 0
    cold = read_part(
        ctx.store, f"marketdata/v1/ticks/instrument={iid}/year=2025/month=01/part-00000.parquet"
    )
    assert cold.column("ts_ns").to_numpy().max() < cutoff
    with engine.connect() as c:
        row = c.execute(sr.instrument.select().where(sr.instrument.c.instrument_id == iid)).one()
    assert row.first_ts_ns is not None and int(row.last_ts_ns) > int(row.first_ts_ns)


def file_engine(_settings: Settings) -> Engine:
    """Picklable engine factory: SQLite schemas in files so worker processes share one hot store."""
    base = Path(os.environ["INGEST_TEST_SQLITE_DIR"])
    eng = create_engine("sqlite://", connect_args={"timeout": 60})

    @event.listens_for(eng, "connect")
    def _attach(dbapi: Any, _rec: object) -> None:
        for schema in ("ref", "md", "ops"):
            dbapi.execute(f"ATTACH DATABASE '{(base / schema).as_posix()}.db' AS {schema}")

    return eng


def test_synth_process_pool_writes_hot_rows_and_matches_serial_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INGEST_TEST_SQLITE_DIR", str(tmp_path))
    now = "2025-01-10T23:59:59+00:00"
    eng = file_engine(Settings.from_env({"TIDAL_ENV": "local"}))
    sr.metadata.create_all(eng)
    ctx = make_ctx(eng, Path(cli.LOCAL_ROOT), now, hot_days=2, env=SYNTH_ENV)
    ctx.engine_factory = file_engine
    units = syn.plan("S", ctx.settings.synth.start, ctx.settings.synth.end)
    pooled = cli.run_synth(ctx, "S", 1, 2, units)
    assert (pooled.groups, pooled.skipped) == (3, 0)
    assert pooled.routed.hot > 0
    assert pooled.routed.cold > 0
    for u in units:  # hot ticks were written by worker processes through the injected engine
        assert ctx.repo.read_ticks(u.instrument_id, 0, ns("2026-01-01")).num_rows > 0
    mem = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(
        mem,
        "connect",
        lambda dbapi, _r: [
            dbapi.execute(f"ATTACH DATABASE ':memory:' AS {s}") for s in ("ref", "md", "ops")
        ],
    )
    sr.metadata.create_all(mem)
    serial = make_ctx(mem, tmp_path / "serial", now, hot_days=2, env=SYNTH_ENV)
    cli.run_synth(serial, "S", 1, 1, units)
    pooled_hashes = tree_hashes(tmp_path / ".data")
    assert pooled_hashes
    assert pooled_hashes == tree_hashes(tmp_path / "serial")


def test_synth_rerun_into_same_store_skips_groups_and_changes_nothing(
    engine: Engine, tmp_path: Path
) -> None:
    ctx = make_ctx(engine, tmp_path, "2025-01-10T23:59:59+00:00", hot_days=2, env=SYNTH_ENV)
    units = syn.plan("S", ctx.settings.synth.start, ctx.settings.synth.end)[:2]
    first = cli.run_synth(ctx, "S", 1, 1, units)
    before = tree_hashes(tmp_path)
    second = cli.run_synth(ctx, "S", 1, 1, units)
    assert (first.groups, first.skipped) == (2, 0)
    assert (second.groups, second.skipped, second.routed) == (0, 2, Routed())
    assert tree_hashes(tmp_path) == before  # identical part set and bytes
    assert not [p for p in before if "part-00001" in p]
    with pytest.raises(ConfigError, match="already exists"):
        cli.run_synth(ctx, "S", 1, 1, units, require_empty=True)


def test_synth_incomplete_earlier_group_is_an_error(engine: Engine, tmp_path: Path) -> None:
    ctx = make_ctx(engine, tmp_path, "2025-01-10T23:59:59+00:00", hot_days=2, env=SYNTH_ENV)
    units = syn.plan("S", ctx.settings.synth.start, ctx.settings.synth.end)[:1]
    cli.run_synth(ctx, "S", 1, 1, units)
    for p in [p for p in tmp_path.rglob("*.parquet") if "bars_1d" in p.as_posix()]:
        p.unlink()  # crashed before the (last-written) 1d part
    with pytest.raises(ConfigError, match="incomplete"):
        cli.run_synth(ctx, "S", 1, 1, units)


def test_synth_command_defaults_and_worker_cap(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(engine, tmp_path, "2030-01-01", hot_days=2, env=SYNTH_ENV)
    seen: dict[str, Any] = {}

    def fake_run(
        _c: Context, scale: str, seed: int, workers: int, *, require_empty: bool = False
    ) -> Any:
        seen.update(scale=scale, seed=seed, workers=workers, require_empty=require_empty)
        return Routed()

    monkeypatch.setattr(cli, "build_context", lambda: ctx)
    monkeypatch.setattr(cli, "run_synth", fake_run)
    assert RUNNER.invoke(cli.synth_app, []).exit_code == 0
    assert (seen["scale"], seen["seed"]) == ("S", ctx.settings.synth.seed)
    assert (
        RUNNER.invoke(cli.synth_app, ["--scale", "M", "--seed", "7", "--workers", "3"]).exit_code
        == 0
    )
    assert seen == {"scale": "M", "seed": 7, "workers": 3, "require_empty": False}
    RUNNER.invoke(cli.synth_app, ["--require-empty", "--workers", "1"])
    assert seen["require_empty"] is True
    assert 1 <= cli.default_workers() <= syn.max_workers_for_memory()


def test_synth_command_reports_typed_errors(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(engine, tmp_path, "2030-01-01", env=SYNTH_ENV)
    monkeypatch.setattr(cli, "build_context", lambda: ctx)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise ConfigError("nope")

    monkeypatch.setattr(cli, "run_synth", boom)
    assert RUNNER.invoke(cli.synth_app, ["--workers", "1"]).exit_code == 1


def test_root_cli_registers_synth_and_not_the_removed_ingest_group() -> None:
    from tidal.cli import build_app

    root = build_app()
    assert RUNNER.invoke(root, ["ingest", "--help"]).exit_code != 0
    out = RUNNER.invoke(root, ["synth", "--help"])
    # CI terminals make rich emit ANSI styling that can split option names; compare plain text.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.output)
    assert out.exit_code == 0 and "--scale" in plain


def test_commands_log_a_correlation_id(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(engine, tmp_path, "2020-04-24")

    def build() -> Context:
        configure_logging(ctx.settings)  # binds the handler to the runner's stdout
        return ctx

    monkeypatch.setattr(cli, "build_context", build)
    result = RUNNER.invoke(cli.synth_app, ["daily-history", "--start", "2020-04-01"])
    assert result.exit_code == 0
    lines = [json.loads(x) for x in result.output.splitlines() if x.startswith("{")]
    assert {x["event"] for x in lines} >= {"daily_history_done"}
    assert len({x["correlation_id"] for x in lines}) == 1


def test_missing_tidal_env_exits_1_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIDAL_ENV", raising=False)
    config.get_settings.cache_clear()
    for argv in ([], ["daily-history"]):
        result = RUNNER.invoke(cli.synth_app, argv)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)


def test_build_store_rejects_connection_string_outside_local() -> None:
    local = Settings.from_env(
        {"TIDAL_ENV": "local", "TIDAL_STORAGE__CONNECTION_STRING": "UseDevelopmentStorage=true"}
    )
    dev = local.model_copy(update={"env": "dev"})
    with pytest.raises(ConfigError, match="TIDAL_ENV=local"):
        cli.build_store(dev)


def test_read_part_selects_columns(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    pq.write_table(pa.table({"a": [1], "b": [2]}), tmp_path / "x.parquet")
    assert read_part(store, "x.parquet", ["b"]).column_names == ["b"]
