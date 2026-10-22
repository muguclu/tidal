"""``tidal synth`` and ``tidal synth daily-history`` (SPEC-01 §3, T-105, T-117).

All data is synthetic (ADR-0013). Pure functions (``run_synth``, ``run_daily_history``) take a
:class:`Context` with injected store, repository and clock; the typer commands only build that
context from settings. Storage is a ``LocalStore('./.data')`` when ``TIDAL_ENV=local`` and no
storage connection string is set, otherwise Azurite / Azure Blob.

Determinism: output is byte-identical across runs only when ``TIDAL_CLOCK__NOW`` is set
(``ingested_at_ns`` comes from the clock). Re-running ``tidal synth`` into a store that already
holds a group skips it (resume); ``--require-empty`` makes that an error instead. Re-running
``daily-history`` is idempotent (unchanged cold bars are skipped).
"""

import multiprocessing
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Final

import pyarrow as pa
import typer
from sqlalchemy import Engine

from tidal.common.config import Settings, get_settings
from tidal.common.errors import ConfigError, TidalError
from tidal.common.logging import (
    bind_correlation_id,
    configure_logging,
    correlation_scope,
    get_correlation_id,
    get_logger,
)
from tidal.common.timeutil import Clock, FixedClock, clock_from_settings
from tidal.contracts.schemas import Interval
from tidal.ingest.router import Routed, Router
from tidal.ingest.sources import synthetic as syn
from tidal.ingest.sources import synthetic_daily as syd
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.aggregate import BarAggregator
from tidal.storage.parquet_writer import AzureBlobStore, LocalStore, ObjectStore

LOCAL_ROOT: Final = Path("./.data")
_SYNTH_INTERVALS: Final = (Interval.S1, Interval.M1, Interval.H1, Interval.D1)
_log = get_logger(__name__)

synth_app = typer.Typer(
    name="synth",
    help="Generate synthetic tick data. Byte-identical output requires TIDAL_CLOCK__NOW.",
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
)


class Scale(StrEnum):
    """Synthetic scales."""

    S = "S"
    M = "M"
    L = "L"


# -- context -------------------------------------------------------------------------------------
@dataclass
class Context:
    """Injected collaborators of the ingest functions."""

    settings: Settings
    store: ObjectStore
    repo: sr.SqlRepository
    clock: Clock
    engine_factory: Callable[[Settings], Engine] = sr.create_engine_from_settings  # worker SQL

    def router(self, *, dedupe_cold: bool) -> Router:
        """Router over this context's tiers and hot window."""
        return Router(
            self.repo,
            self.store,
            self.clock,
            self.settings.hot_window_days,
            dedupe_cold=dedupe_cold,
        )


def build_store(settings: Settings) -> ObjectStore:
    """Local directory store, Azurite (connection string) or Azure Blob (managed identity)."""
    conn = settings.storage.connection_string
    if conn is not None and settings.env != "local":
        raise ConfigError("a storage connection string is only allowed when TIDAL_ENV=local")
    if conn is None and settings.env == "local":
        return LocalStore(LOCAL_ROOT)
    from azure.storage.blob import BlobServiceClient  # noqa: PLC0415 - only needed off-local

    if conn is not None:
        service = BlobServiceClient.from_connection_string(conn.get_secret_value())
        for name in (layout.CONTAINER_MARKETDATA, layout.CONTAINER_RAW):
            if not service.get_container_client(name).exists():
                service.create_container(name)
    else:
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415

        service = BlobServiceClient(settings.storage.account_url, DefaultAzureCredential())
    return AzureBlobStore(service)


def build_context(settings: Settings | None = None) -> Context:
    """Context from process settings (logging configured, SQL engine lazily connected)."""
    settings = settings or get_settings()
    configure_logging(settings)
    clock = clock_from_settings(settings)
    repo = sr.SqlRepository(sr.create_engine_from_settings(settings), clock)
    return Context(settings, build_store(settings), repo, clock)


# -- synthetic -----------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GroupResult:
    """Rows written per tier and the instrument bounds of one (instrument, year) work group."""

    key: str
    routed: Routed
    bounds: dict[str, tuple[int, int]]


def _hash(seed: int, scale: str, *parts: object) -> str:
    return sha256(f"syn:{seed}:{scale}:{parts}".encode()).hexdigest()


def _synth_month(
    router: Router, unit: syn.WorkUnit, seed: int, scale: str
) -> tuple[Routed, list[pa.Table]]:
    """One instrument-month: stream ticks and 1s/1m/1h bars; return rows written + 1d bars."""
    h = _hash(seed, scale, unit)
    agg = BarAggregator(_SYNTH_INTERVALS)
    bars: dict[Interval, list[pa.Table]] = {i: [] for i in _SYNTH_INTERVALS}

    def ticks() -> Iterator[pa.Table]:
        for t in syn.generate_unit(unit, seed=seed, scale=scale, clock=router.clock):
            for iv, tabs in agg.feed(t).items():
                bars[iv].extend(tabs)
            yield t

    def write(dataset: str, batches: Iterable[pa.Table]) -> Routed:
        return router.write_stream(
            dataset,
            instrument_id=unit.instrument_id,
            year=unit.year,
            month=unit.month,
            batches=batches,
            source_hash=h,
        )

    routed = write("ticks", ticks())
    for iv, tabs in agg.flush().items():
        bars[iv].extend(tabs)
    daily = bars.pop(Interval.D1)
    for iv, tabs in bars.items():
        routed += write(f"bars_{iv.value}", tabs)
    return routed, daily


def synth_group(
    router: Router, units: Sequence[syn.WorkUnit], seed: int, scale: str
) -> GroupResult:
    """Generate the months of ONE instrument-year, in order.

    The year's ``1d`` bars are written once at the end as a single ``month=00`` part, so
    concurrent groups never share a partition (parts are numbered per partition: that would race).
    """
    iid, year = units[0].instrument_id, units[0].year
    daily: list[pa.Table] = []
    routed = Routed()
    for unit in units:
        month_routed, month_daily = _synth_month(router, unit, seed, scale)
        routed += month_routed
        daily.extend(month_daily)
    routed += router.write_stream(
        "bars_1d",
        instrument_id=iid,
        year=year,
        month=0,
        batches=daily,
        source_hash=_hash(seed, scale, iid, year),
    )
    return GroupResult(f"{iid}/{year}", routed, dict(router.bounds))


def _groups(units: Sequence[syn.WorkUnit]) -> list[list[syn.WorkUnit]]:
    """Units grouped by (instrument, year), keeping plan order."""
    groups: dict[tuple[str, int], list[syn.WorkUnit]] = {}
    for u in units:
        groups.setdefault((u.instrument_id, u.year), []).append(u)
    return list(groups.values())


def _worker(
    settings: Settings,
    *,
    now_ns: int,
    units: list[syn.WorkUnit],
    seed: int,
    scale: str,
    correlation_id: str | None,
    engine_factory: Callable[[Settings], Engine],
) -> GroupResult:
    """Process-pool entry: rebuild collaborators from the pickled settings."""
    configure_logging(settings)  # redaction + JSON in the child as well
    bind_correlation_id(correlation_id)
    clock = FixedClock(now_ns)
    repo = sr.SqlRepository(engine_factory(settings), clock)
    router = Router(repo, build_store(settings), clock, settings.hot_window_days)
    return synth_group(router, units, seed, scale)


def default_workers() -> int:
    """CPU count capped by the 2 GB memory budget (``max_workers_for_memory``)."""
    return min(os.cpu_count() or 1, syn.max_workers_for_memory())


@dataclass(frozen=True, slots=True)
class SynthResult:
    """Rows written plus how many (instrument, year) groups ran or were skipped as present."""

    routed: Routed
    groups: int
    skipped: int


def _group_state(store: ObjectStore, units: Sequence[syn.WorkUnit]) -> str:
    """``done`` (its 1d part, written last, exists), ``partial`` (other parts) or ``empty``."""
    iid, year = units[0].instrument_id, units[0].year

    def has(dataset: str, month: int) -> bool:
        prefix = layout.md_partition_prefix(dataset, iid, year, month)
        return any(p.endswith(".parquet") for p in store.list(prefix))

    if has("bars_1d", 0):
        return "done"
    months = {u.month for u in units}
    others = ("ticks", "bars_1s", "bars_1m", "bars_1h")
    return "partial" if any(has(d, m) for m in months for d in others) else "empty"


def run_synth(
    ctx: Context,
    scale: str,
    seed: int,
    workers: int,
    units: Sequence[syn.WorkUnit] | None = None,
    *,
    require_empty: bool = False,
) -> SynthResult:
    """Plan and generate synthetic data; ``workers`` is capped by ``max_workers_for_memory``.

    Groups already present in the store are skipped (resume, no duplicate parts); with
    ``require_empty`` that is an error instead. A group with parts but no ``bars_1d`` part is a
    crashed earlier run and must be removed by hand (``ConfigError``).
    """
    s = ctx.settings.synth
    todo = list(units) if units is not None else syn.plan(scale, s.start, s.end)
    all_groups = _groups(todo)
    groups = []
    for g in all_groups:
        state = _group_state(ctx.store, g)
        key = f"{g[0].instrument_id}/{g[0].year}"
        if state == "partial":
            raise ConfigError(f"{key} has parts from an incomplete earlier run; remove them first")
        if state == "done" and require_empty:
            raise ConfigError(f"{key} already exists in the target store")
        if state == "done":
            _log.info("synth_group_skipped", group=key)
        else:
            groups.append(g)
    skipped = len(all_groups) - len(groups)
    router = ctx.router(dedupe_cold=False)
    router.ensure_instruments({u.instrument_id for g in groups for u in g})
    workers = max(1, min(workers, syn.max_workers_for_memory(), len(groups) or 1))
    # Frozen run clock: every unit stamps the same ingested_at_ns (deterministic parts).
    run_clock = FixedClock(ctx.clock.now_ns())
    router.clock = run_clock
    total = Routed()
    _log.info(
        "synth_start", scale=scale, seed=seed, groups=len(groups), skipped=skipped, workers=workers
    )
    if workers == 1:
        results = (synth_group(router, g, seed, scale) for g in groups)
    else:
        cid = get_correlation_id()
        # Spawn everywhere: fork after polars/pyarrow threads start can deadlock (Linux CI).
        pool = ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        )
        futures = [
            pool.submit(
                _worker,
                ctx.settings,
                now_ns=run_clock.now_ns(),
                units=g,
                seed=seed,
                scale=scale,
                correlation_id=cid,
                engine_factory=ctx.engine_factory,
            )
            for g in groups
        ]
        results = (f.result() for f in futures)
    try:
        for res in results:
            total += res.routed
            router.note_bounds(res.bounds)
            _log.info("synth_group_done", group=res.key, hot=res.routed.hot, cold=res.routed.cold)
    finally:
        if workers > 1:
            pool.shutdown(wait=True, cancel_futures=True)
    router.flush_bounds()
    _log.info("synth_done", groups=len(groups), skipped=skipped, hot=total.hot, cold=total.cold)
    return SynthResult(total, len(groups), skipped)


@dataclass(frozen=True, slots=True)
class DailyHistoryResult:
    """Rows written per tier for ``daily-history`` and the covered trade-date range."""

    routed: Routed
    instruments: int
    start: date
    end: date


def run_daily_history(ctx: Context, seed: int, start: date) -> DailyHistoryResult:
    """Write synthetic daily bars of the daily catalogue from ``start`` to the day before "now".

    Goes through the normal hot/cold router (``dedupe_cold``: a re-run writes nothing new). The
    last trade date is the clock's UTC date minus one (a daily bar is complete after its day).
    """
    end = ctx.clock.now().date() - timedelta(days=1)
    if start > end:
        raise ConfigError(f"daily history start {start} is after the last complete day {end}")
    ids = syd.daily_instruments()
    router = ctx.router(dedupe_cold=True)
    router.ensure_instruments(ids)
    total = Routed()
    for iid in ids:
        bars = syd.generate_daily(iid, seed=seed, start=start, end=end, clock=ctx.clock)
        digest = sha256(f"syn-daily:{seed}:{iid}:{start}:{end}".encode()).hexdigest()
        total += router.route_bars(bars, digest)
    router.flush_bounds()
    _log.info("daily_history_done", instruments=len(ids), hot=total.hot, cold=total.cold)
    return DailyHistoryResult(total, len(ids), start, end)


# -- typer commands ------------------------------------------------------------------------------
def _date(value: str | None) -> date | None:
    try:
        return None if value is None else date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not an ISO date (YYYY-MM-DD)") from None


def _fail(exc: TidalError) -> typer.Exit:
    _log.error("ingest_failed", error=exc.slug, detail=str(exc))
    return typer.Exit(1)


@synth_app.callback(invoke_without_command=True)
def synth_cmd(
    typer_ctx: typer.Context,
    scale: Annotated[Scale | None, typer.Option(help="S, M or L")] = None,
    seed: Annotated[int | None, typer.Option(help="Root seed")] = None,
    workers: Annotated[int | None, typer.Option(help="Parallel workers")] = None,
    require_empty: Annotated[
        bool,
        typer.Option(
            "--require-empty", help="Fail if a group already exists instead of skipping it"
        ),
    ] = False,
) -> None:
    """Generate synthetic ticks and bars (range from TIDAL_SYNTH__START/END).

    Byte-identical output across runs requires TIDAL_CLOCK__NOW. Groups already in the store
    are skipped (resume).
    """
    if typer_ctx.invoked_subcommand is not None:
        return
    with correlation_scope():
        try:
            ctx = build_context()
            s = ctx.settings.synth
            run_synth(
                ctx,
                (scale or Scale(s.scale)).value,
                s.seed if seed is None else seed,
                workers or default_workers(),
                require_empty=require_empty,
            )
        except TidalError as exc:
            raise _fail(exc) from exc


@synth_app.command("daily-history")
def daily_history_cmd(
    start: Annotated[
        str | None, typer.Option(help="First trade date YYYY-MM-DD (default TIDAL_SYNTH__START)")
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Root seed (default TIDAL_SYNTH__SEED)")] = None,
) -> None:
    """Generate synthetic daily bars (no ticks) for the daily catalogue up to the clock.

    Spot, futures M1..M4 and EUR/USD, deterministic per seed, including a WTI negative-price
    episode. Byte-identical output across runs requires TIDAL_CLOCK__NOW.
    """
    with correlation_scope():
        try:
            ctx = build_context()
            first = _date(start) or ctx.settings.synth.start
            run_daily_history(ctx, ctx.settings.synth.seed if seed is None else seed, first)
        except TidalError as exc:
            raise _fail(exc) from exc
