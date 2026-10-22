# ruff: noqa: PT018, PLC0415
"""CLI-level acceptance tests (SPEC-01 §5) on SQLite + LocalStore with MockTransport upstreams."""

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from tidal.common import config
from tidal.common.config import Settings
from tidal.common.errors import ConfigError
from tidal.common.logging import configure_logging
from tidal.common.timeutil import FixedClock, to_ts_ns
from tidal.contracts.schemas import Interval, QualityFlag
from tidal.ingest import cli
from tidal.ingest.cli import Context
from tidal.ingest.router import Routed, read_part
from tidal.ingest.sources import synthetic as syn
from tidal.ingest.sources.eia import EiaRequestRejected, EiaSource
from tidal.ingest.sources.fred import FredSource
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import AzureBlobStore, LocalStore

FIX = Path(__file__).parents[2] / "fixtures"
DAY = 86_400 * 10**9
WTI, FUT1 = "EIA.WTI.SPOT", "EIA.WTI.FUT1"
RUNNER = CliRunner()


def ns(day: str) -> int:
    parsed = datetime.fromisoformat(day)
    return to_ts_ns(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))


class Upstream:
    """MockTransport handler: EIA v2 and FRED endpoints over mutable in-memory series."""

    def __init__(self, eia: Any, fred: Any):
        self.eia, self.fred = eia, fred
        self.page_size = 5000
        self.fail_from_offset: int | None = None
        self.lie_about_total = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        q = request.url.params
        offset = int(q["offset"])
        if self.fail_from_offset is not None and offset >= self.fail_from_offset:
            return httpx.Response(400)
        if request.url.host == "api.eia.gov":
            series, lo, hi = q["facets[series][]"], q["start"], q["end"]
            rows = [(d, v) for d, v in sorted(self.eia[series].items()) if lo <= d <= hi]
            body: dict[str, Any] = {
                "response": {
                    "total": str(len(rows) + self.lie_about_total),
                    "data": [
                        {"period": d, "series": series, "value": None if v is None else str(v)}
                        for d, v in rows[offset : offset + self.page_size]
                    ],
                },
                "request": {},
            }
        else:
            series, lo, hi = q["series_id"], q["observation_start"], q["observation_end"]
            obs = [(d, v) for d, v in sorted(self.fred[series].items()) if lo <= d <= hi]
            body = {
                "count": len(obs) + self.lie_about_total,
                "observations": [
                    {"date": d, "value": v} for d, v in obs[offset : offset + self.page_size]
                ],
            }
        return httpx.Response(200, content=json.dumps(body).encode())


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
    upstream: Upstream | None = None,
    *,
    hot_days: int = 90,
    env: dict[str, str] | None = None,
) -> Context:
    settings = Settings.from_env(
        {"TIDAL_ENV": "local", "TIDAL_HOT_WINDOW_DAYS": str(hot_days), **(env or {})}
    )
    clock = FixedClock(ns(now))
    store = LocalStore(root)
    sources: dict[str, Any] = {}
    if upstream is not None:
        client = httpx.Client(transport=httpx.MockTransport(upstream))
        rng = np.random.Generator(np.random.PCG64(1))
        key = SecretStr("k")
        sources = {
            "EIA": EiaSource(client, key, store, rng, clock=clock, sleep=lambda _s: None),
            "FRED": FredSource(client, key, store, rng, clock=clock, sleep=lambda _s: None),
        }
    return Context(settings, store, sr.SqlRepository(engine, clock), clock, sources)


def adv(ctx: Context) -> None:
    assert isinstance(ctx.clock, FixedClock)
    ctx.clock.advance(DAY)


def fixture_upstream() -> Upstream:
    """WTI spot/FUT1 around the 2020-04-20 negative print plus FRED EUR/USD (with a '.')."""
    eia = {
        "RWTC": {
            "2020-04-16": 28.34, "2020-04-17": 18.27, "2020-04-20": -36.98,
            "2020-04-21": 8.91, "2020-04-22": 13.75, "2020-04-23": 15.06,
        },
        "RCLC1": {"2020-04-20": -37.63},
    }  # fmt: skip
    fred = {
        "DEXUSEU": {
            "1999-01-04": "1.1812", "1999-01-05": "1.1760", "1999-01-06": "1.1636",
            "1999-01-07": "1.1672", "1999-01-08": ".", "1999-01-11": "1.1554",
        }
    }  # fmt: skip
    return Upstream(eia, fred)


def test_fixtures_agree_with_the_mock_upstream() -> None:
    up = fixture_upstream()
    page1 = json.loads((FIX / "eia" / "rwtc_page1.json").read_bytes())["response"]["data"]
    assert {r["period"]: float(r["value"]) for r in page1}.items() <= up.eia["RWTC"].items()


def test_golden_wti_negative_print_spot_and_fut1(engine: Engine, tmp_path: Path) -> None:
    ctx = make_ctx(engine, tmp_path, "2020-04-24", fixture_upstream())
    results = cli.backfill(ctx, "eia", None, [WTI, FUT1])
    assert all(r.ok for r in results)
    day = ns("2020-04-20")
    for iid, price in ((WTI, -36.98), (FUT1, -37.63)):
        bars = ctx.repo.read_bars(iid, Interval.D1, day, day + DAY)
        assert bars.column("close").to_pylist() == [price]
        assert int(bars.column("quality")[0].as_py() or 0) & QualityFlag.NEGATIVE
        assert bars.column("version").to_pylist() == [1]
    with engine.connect() as c:
        row = c.execute(sr.instrument.select().where(sr.instrument.c.instrument_id == WTI)).one()
    assert (row.first_ts_ns, row.last_ts_ns) == (ns("2020-04-16"), ns("2020-04-23"))
    # raw pages were stored before normalisation
    assert any(p.startswith("raw/eia/rwtc/") for p in ctx.store.list_paths("raw/"))


def test_golden_negative_print_in_cold_parquet(engine: Engine, tmp_path: Path) -> None:
    ctx = make_ctx(engine, tmp_path, "2021-01-01", fixture_upstream(), hot_days=30)
    cli.backfill(ctx, "eia", None, [WTI])
    [path] = [p for p in ctx.store.list_paths("marketdata/") if p.endswith(".parquet")]
    assert path == f"marketdata/v1/bars_1d/instrument={WTI}/year=2020/month=00/part-00000.parquet"
    table = read_part(ctx.store, path)
    i = table.column("ts_ns").to_pylist().index(ns("2020-04-20"))
    assert table.column("close")[i].as_py() == -36.98
    assert table.column("quality")[i].as_py() & QualityFlag.NEGATIVE


def test_backfill_logs_counts_and_flags_total_mismatch(engine: Engine, tmp_path: Path) -> None:
    up = fixture_upstream()
    ctx = make_ctx(engine, tmp_path, "2020-04-24", up)
    [res] = cli.backfill(ctx, "fred", date(1999, 1, 1), ["FRED.EURUSD.FX"])
    assert (res.rows, res.total, res.nan_dropped, res.ok) == (6, 6, 1, True)
    assert res.written.hot == 0 and res.written.cold == 5  # 1999 is outside the 90 day window
    up.lie_about_total = 1
    ctx2 = make_ctx(engine, tmp_path / "b", "2020-04-24", up)
    [bad] = cli.backfill(ctx2, "fred", date(1999, 1, 1), ["FRED.EURUSD.FX"])
    assert not bad.ok


def test_backfill_resumes_after_crash_from_checkpoint(engine: Engine, tmp_path: Path) -> None:
    up = fixture_upstream()
    up.page_size, up.fail_from_offset = 2, 2  # second page request fails: crash mid-series
    ctx = make_ctx(engine, tmp_path, "2020-04-24", up)
    with pytest.raises(EiaRequestRejected):
        cli.backfill(ctx, "eia", None, [WTI])
    assert ctx.repo.get_checkpoint("EIA", WTI) == ns("2020-04-17")
    assert ctx.repo.read_bars(WTI, Interval.D1, 0, ns("2021-01-01")).num_rows == 2

    up.fail_from_offset, up.requests = None, []
    [res] = cli.backfill(ctx, "eia", None, [WTI])
    assert up.requests[0].url.params["start"] == "2020-04-18"  # completed pages are skipped
    assert (res.rows, res.total) == (4, 4)
    assert ctx.repo.get_checkpoint("EIA", WTI) == ns("2020-04-23")
    assert ctx.repo.read_bars(WTI, Interval.D1, 0, ns("2021-01-01")).num_rows == 6
    assert cli.backfill(ctx, "eia", None, [WTI])[0].rows == 0  # nothing left after the last day


def test_backfill_start_after_end_is_skipped_and_unknown_instrument_rejected(
    engine: Engine, tmp_path: Path
) -> None:
    ctx = make_ctx(engine, tmp_path, "2020-04-24", fixture_upstream())
    assert cli.backfill(ctx, "eia", date(2030, 1, 1), [WTI]) == []
    with pytest.raises(ConfigError):
        cli.backfill(ctx, "eia", None, ["FRED.EURUSD.FX"])


def daily_upstream() -> Upstream:
    days = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"]
    series = {s: {d: 50.0 + i + n for n, d in enumerate(days)} for i, s in enumerate(
        ("RBRTE", "RWTC", "RNGWHHD")
    )}  # fmt: skip
    return Upstream(series, {"DEXUSEU": {d: f"1.{n}0" for n, d in enumerate(days)}})


def test_daily_is_idempotent_then_restatement_gets_version_2_with_as_of(
    engine: Engine, tmp_path: Path
) -> None:
    up = daily_upstream()
    ctx = make_ctx(engine, tmp_path, "2026-03-10T22:15:00+00:00", up)
    first = cli.daily(ctx)
    assert sum(r.written.hot for r in first) == 24 and all(r.ok for r in first)
    assert all("FUT" not in r.instrument_id for r in first)  # futures are backfill-only
    t_before = ctx.clock.now_ns()

    again = cli.daily(ctx)
    assert sum(r.written.hot + r.written.cold for r in again) == 0
    assert [r.rows for r in again] == [r.rows for r in first]

    adv(ctx)
    up.eia["RWTC"]["2026-03-05"] = 99.5
    restated = cli.daily(ctx)
    assert sum(r.written.hot for r in restated) == 1
    day = ns("2026-03-05")
    latest = ctx.repo.read_bars(WTI, Interval.D1, day, day + DAY)
    assert latest.column("close").to_pylist() == [99.5]
    assert latest.column("version").to_pylist() == [2]
    assert int(latest.column("quality")[0].as_py() or 0) & QualityFlag.RESTATED
    old = ctx.repo.read_bars(WTI, Interval.D1, day, day + DAY, as_of_ns=t_before)
    assert old.column("version").to_pylist() == [1]
    assert old.column("close").to_pylist() == [54.0]


def test_daily_requires_hot_window_covering_the_lookback(engine: Engine, tmp_path: Path) -> None:
    ctx = make_ctx(engine, tmp_path, "2026-03-10", daily_upstream(), hot_days=5)
    with pytest.raises(ConfigError, match="TIDAL_HOT_WINDOW_DAYS"):
        cli.daily(ctx)


# -- typer commands ------------------------------------------------------------------------------
def test_backfill_command_uses_context_and_exit_codes(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    up = fixture_upstream()
    ctx = make_ctx(engine, tmp_path, "2020-04-24", up)
    monkeypatch.setattr(cli, "build_context", lambda: ctx)
    args = ["backfill", "--source", "eia", "--from", "2020-04-01", "--instrument", WTI]
    assert RUNNER.invoke(cli.app, args).exit_code == 0
    assert ctx.repo.read_bars(WTI, Interval.D1, 0, ns("2021-01-01")).num_rows == 6
    bad = RUNNER.invoke(cli.app, ["backfill", "--source", "eia", "--from", "yesterday"])
    assert bad.exit_code == 2
    unknown = RUNNER.invoke(cli.app, ["backfill", "--source", "eia", "--instrument", "FRED.X.Y"])
    assert unknown.exit_code == 1
    up.lie_about_total = 1
    ctx2 = make_ctx(engine, tmp_path / "b", "2020-04-24", up)
    monkeypatch.setattr(cli, "build_context", lambda: ctx2)
    assert (
        RUNNER.invoke(cli.app, ["backfill", "--source", "eia", "--instrument", WTI]).exit_code == 1
    )


def test_daily_command_and_entrypoint(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    up = daily_upstream()
    ctx = make_ctx(engine, tmp_path, "2026-03-10", up)
    monkeypatch.setattr(cli, "build_context", lambda: ctx)
    assert RUNNER.invoke(cli.app, ["daily"]).exit_code == 0
    assert ctx.repo.read_bars(WTI, Interval.D1, 0, ns("2027-01-01")).num_rows == 6
    up.lie_about_total = 2
    assert RUNNER.invoke(cli.app, ["daily"]).exit_code == 1

    called: list[list[str]] = []
    monkeypatch.setattr(cli, "app", called.append)
    cli.run_daily()
    assert called == [["daily"]]


def test_daily_command_reports_typed_errors(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(engine, tmp_path, "2026-03-10")  # no injected sources, no API keys
    monkeypatch.setattr(cli, "build_context", lambda: ctx)
    assert RUNNER.invoke(cli.app, ["daily"]).exit_code == 1


# -- storage / context ---------------------------------------------------------------------------
def test_build_store_local_default_and_azure_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    store = cli.build_store(Settings.from_env({"TIDAL_ENV": "local"}))
    assert isinstance(store, LocalStore) and store.root == cli.LOCAL_ROOT

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
    assert created == ["marketdata"]

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


def test_real_sources_built_from_settings_when_not_injected(engine: Engine, tmp_path: Path) -> None:
    env = {"TIDAL_SOURCES__EIA_API_KEY": "e", "TIDAL_SOURCES__FRED_API_KEY": "f"}
    ctx = make_ctx(engine, tmp_path, "2026-03-10", env=env)
    got = cli._real_sources(ctx, ["EIA", "FRED"])
    assert isinstance(got["EIA"], EiaSource) and isinstance(got["FRED"], FredSource)


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


def test_root_cli_registers_ingest_and_synth() -> None:
    from tidal.cli import build_app

    root = build_app()
    assert RUNNER.invoke(root, ["ingest", "--help"]).exit_code == 0
    out = RUNNER.invoke(root, ["synth", "--help"])
    # CI terminals make rich emit ANSI styling that can split option names; compare plain text.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.output)
    assert out.exit_code == 0 and "--scale" in plain


def test_commands_log_a_correlation_id(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(engine, tmp_path, "2020-04-24", fixture_upstream())

    def build() -> Context:
        configure_logging(ctx.settings)  # binds the handler to the runner's stdout
        return ctx

    monkeypatch.setattr(cli, "build_context", build)
    result = RUNNER.invoke(cli.app, ["backfill", "--source", "eia", "--instrument", WTI])
    assert result.exit_code == 0
    lines = [json.loads(x) for x in result.output.splitlines() if x.startswith("{")]
    events = {x["event"]: x for x in lines}
    assert events["series_ingested"]["correlation_id"]
    assert len({x["correlation_id"] for x in lines}) == 1


def test_missing_tidal_env_exits_1_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIDAL_ENV", raising=False)
    config.get_settings.cache_clear()
    for command, argv in (
        (cli.app, ["daily"]),
        (cli.synth_app, []),
        (cli.app, ["backfill", "--source", "eia"]),
    ):
        result = RUNNER.invoke(command, argv)
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
