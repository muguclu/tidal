"""``tidal ingest backfill|daily`` and ``tidal synth`` (SPEC-01 §3, T-105).

Pure functions (``backfill``, ``daily``, ``run_synth``) take a :class:`Context` with injected
store, repository, clock and sources; the typer commands only build that context from settings.
Storage is a ``LocalStore('./.data')`` when ``TIDAL_ENV=local`` and no storage connection string
is set, otherwise Azurite / Azure Blob.

Determinism: ``tidal synth`` output is byte-identical across runs only when ``TIDAL_CLOCK__NOW`` is
set (``ingested_at_ns`` comes from the clock). Re-running into a store that already holds a group
skips it (resume); ``--require-empty`` makes that an error instead.

NEEDS-KEY (gate G1, not yet run): the 20+ year real-data backfills need
``TIDAL_SOURCES__EIA_API_KEY`` and ``TIDAL_SOURCES__FRED_API_KEY``. Once the keys exist, run::

    tidal ingest backfill --source eia
    tidal ingest backfill --source fred

``tidal ingest daily`` needs ``TIDAL_HOT_WINDOW_DAYS >= 10`` (its look-back).
"""

import json
import logging
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Final

import httpx
import numpy as np
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
from tidal.common.timeutil import Clock, FixedClock, clock_from_settings, from_ts_ns
from tidal.contracts.instruments import CATALOGUE_V1, InstrumentKind
from tidal.contracts.schemas import Interval
from tidal.ingest.normalise import normalise_page
from tidal.ingest.router import Routed, Router
from tidal.ingest.sources import synthetic as syn
from tidal.ingest.sources.base import Source, series_for
from tidal.ingest.sources.eia import EiaSource, parse_eia_page
from tidal.ingest.sources.fred import FredSource, parse_fred_page
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.aggregate import BarAggregator
from tidal.storage.parquet_writer import AzureBlobStore, LocalStore, ObjectStore

DEFAULT_FROM: Final = date(1980, 1, 1)
DAILY_LOOKBACK_DAYS: Final = 10
LOCAL_ROOT: Final = Path("./.data")
_SYNTH_INTERVALS: Final = (Interval.S1, Interval.M1, Interval.H1, Interval.D1)
_log = get_logger(__name__)

app = typer.Typer(
    name="ingest",
    help="Ingest EIA / FRED data. Real backfills need API keys (NEEDS-KEY): "
    "`tidal ingest backfill --source eia` / `--source fred`.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
synth_app = typer.Typer(
    name="synth",
    help="Generate synthetic tick data. Byte-identical output requires TIDAL_CLOCK__NOW.",
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
)


class SourceName(StrEnum):
    """Real data sources."""

    EIA = "eia"
    FRED = "fred"


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
    sources: Mapping[str, Source] = field(default_factory=dict)
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


def _http() -> httpx.Client:
    return httpx.Client(timeout=30.0)


def _real_sources(ctx: Context, names: Sequence[str]) -> dict[str, Source]:
    """EIA / FRED clients (unless injected in ``ctx.sources``) with a seeded jitter stream."""
    rng = np.random.Generator(np.random.PCG64(ctx.settings.synth.seed))
    src = ctx.settings.sources
    makers: dict[str, Callable[[], Source]] = {
        "EIA": lambda: EiaSource(_http(), src.eia_api_key, ctx.store, rng, clock=ctx.clock),
        "FRED": lambda: FredSource(_http(), src.fred_api_key, ctx.store, rng, clock=ctx.clock),
    }
    return {n: ctx.sources.get(n) or makers[n]() for n in names}


# -- backfill / daily ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SeriesResult:
    """Outcome of one instrument: rows fetched vs the source's ``total``, rows written."""

    instrument_id: str
    rows: int
    total: int | None
    nan_dropped: int
    written: Routed

    @property
    def ok(self) -> bool:
        """``False`` when the fetched row count disagrees with the source total."""
        return self.total is None or self.rows == self.total


def _source_total(source: str, payload: bytes) -> int | None:
    try:
        body = json.loads(payload)
        return int(body["response"]["total"] if source == "EIA" else body["count"])
    except (ValueError, KeyError, TypeError):
        return None


def ingest_series(
    ctx: Context,
    router: Router,
    source: str,
    src: Source,
    *,
    instrument_id: str,
    start: date,
    end: date,
    checkpoint: bool,
) -> SeriesResult:
    """Fetch, normalise and route one instrument; checkpoint after each routed page."""
    series = series_for(instrument_id)
    rows = dropped = 0
    total: int | None = None
    written = Routed()
    for chunk in src.fetch(instrument_id, start, end):
        page = (
            parse_eia_page(chunk.payload)
            if source == "EIA"
            else parse_fred_page(chunk.payload, series)
        )
        if chunk.page == 0:
            total = _source_total(source, chunk.payload)
        norm = normalise_page(page, source, ctx.clock)
        written += router.route_bars(norm.bars, chunk.sha256)
        rows += norm.rows_in
        dropped += norm.nan_dropped
        if checkpoint and page.num_rows:
            ctx.repo.set_checkpoint(source, instrument_id, int(page.column("ts_ns")[-1].as_py()))
    result = SeriesResult(instrument_id, rows, total, dropped, written)
    _log.log(
        logging.INFO if result.ok else logging.ERROR,
        "series_ingested",
        instrument_id=instrument_id,
        rows=rows,
        source_total=total,
        nan_dropped=dropped,
        hot_written=written.hot,
        cold_written=written.cold,
    )
    return result


def _instruments(source: str, only: Sequence[str] = ()) -> list[str]:
    ids = [e.instrument_id for e in CATALOGUE_V1 if e.instrument_id.startswith(source + ".")]
    unknown = set(only) - set(ids)
    if unknown:
        raise ConfigError(f"not a {source} instrument: {', '.join(sorted(unknown))}")
    return [i for i in ids if not only or i in only]


def backfill(
    ctx: Context, source: str, start: date | None = None, instruments: Sequence[str] = ()
) -> list[SeriesResult]:
    """Resumable backfill: start after ``ops.ingest_checkpoint`` (never before ``start``)."""
    source = source.upper()
    src = _real_sources(ctx, [source])[source]
    router = ctx.router(dedupe_cold=True)
    ids = _instruments(source, instruments)
    router.ensure_instruments(ids)
    end = ctx.clock.now().date()
    results = []
    for iid in ids:
        first = start or DEFAULT_FROM
        cp = ctx.repo.get_checkpoint(source, iid)
        if cp is not None:
            first = max(first, from_ts_ns(cp).date() + timedelta(days=1))
            _log.info("backfill_resume", instrument_id=iid, resume_from=first.isoformat())
        if first > end:
            continue
        results.append(
            ingest_series(
                ctx, router, source, src, instrument_id=iid, start=first, end=end, checkpoint=True
            )
        )
    router.flush_bounds()
    return results


def daily(ctx: Context) -> list[SeriesResult]:
    """Last 10 days of every non-futures EIA/FRED instrument; restated values get version+1.

    Requires ``hot_window_days >= DAILY_LOOKBACK_DAYS`` so restatements always hit the hot store
    (never cold rows written as v1 duplicates of SQL-resident data).
    """
    if ctx.settings.hot_window_days < DAILY_LOOKBACK_DAYS:
        raise ConfigError(f"TIDAL_HOT_WINDOW_DAYS must be >= {DAILY_LOOKBACK_DAYS} for daily")
    ids = [
        e.instrument_id
        for e in CATALOGUE_V1
        if e.source.value in ("EIA", "FRED") and e.kind is not InstrumentKind.FUTURE
    ]
    names = sorted({i.split(".")[0] for i in ids})
    sources = _real_sources(ctx, names)
    router = ctx.router(dedupe_cold=True)
    router.ensure_instruments(ids)
    end = ctx.clock.now().date()
    start = end - timedelta(days=DAILY_LOOKBACK_DAYS)
    results = []
    for iid in ids:
        name = iid.split(".")[0]
        results.append(
            ingest_series(
                ctx, router, name, sources[name], instrument_id=iid, start=start, end=end,
                checkpoint=False,
            )
        )  # fmt: skip
    router.flush_bounds()
    return results


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
        return any(p.endswith(".parquet") for p in store.list_paths(prefix))

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
        pool = ProcessPoolExecutor(max_workers=workers)
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


# -- typer commands ------------------------------------------------------------------------------
def _date(value: str | None) -> date | None:
    try:
        return None if value is None else date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not an ISO date (YYYY-MM-DD)") from None


def _fail(exc: TidalError) -> typer.Exit:
    _log.error("ingest_failed", error=exc.slug, detail=str(exc))
    return typer.Exit(1)


@app.command("backfill")
def backfill_cmd(
    source: Annotated[SourceName, typer.Option(help="eia or fred")],
    from_: Annotated[str | None, typer.Option("--from", help="Start date YYYY-MM-DD")] = None,
    instrument: Annotated[list[str] | None, typer.Option(help="Instrument id (repeatable)")] = None,
) -> None:
    """Backfill history (needs the source API key); resumes from ``ops.ingest_checkpoint``."""
    start = _date(from_)
    with correlation_scope():
        try:
            results = backfill(build_context(), source.value, start, instrument or ())
        except TidalError as exc:
            raise _fail(exc) from exc
    if not all(r.ok for r in results):
        raise typer.Exit(1)


@app.command("daily")
def daily_cmd() -> None:
    """Re-fetch the last 10 days of EIA/FRED spot and FX series; restated values become v+1."""
    with correlation_scope():
        try:
            results = daily(build_context())
        except TidalError as exc:
            raise _fail(exc) from exc
    if not all(r.ok for r in results):
        raise typer.Exit(1)


@synth_app.callback(invoke_without_command=True)
def synth_cmd(
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


def run_daily() -> None:
    """Console-script entry point ``tidal-ingest`` (Container Apps Job): ``tidal ingest daily``."""
    app(["daily"])
